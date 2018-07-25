from __future__ import division
import torch
import torch.nn as nn
import torch.nn.functional as F

from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack

import onmt
from onmt.Utils import aeq


def rnn_factory(rnn_type, **kwargs):
    # Use pytorch version when available.
    no_pack_padded_seq = False
    if rnn_type == "SRU":
        # SRU doesn't support PackedSequence.
        no_pack_padded_seq = True
        rnn = onmt.modules.SRU(**kwargs)
    else:
        rnn = getattr(nn, rnn_type)(**kwargs)
    return rnn, no_pack_padded_seq


class EncoderBase(nn.Module):
    """
    Base encoder class. Specifies the interface used by different encoder types
    and required by :obj:`onmt.Models.NMTModel`.

    .. mermaid::

       graph BT
          A[Input]
          subgraph RNN
            C[Pos 1]
            D[Pos 2]
            E[Pos N]
          end
          F[Memory_Bank]
          G[Final]
          A-->C
          A-->D
          A-->E
          C-->F
          D-->F
          E-->F
          E-->G
    """
    def _check_args(self, input, lengths=None, hidden=None):
        s_len, n_batch, n_feats = input.size()
        if lengths is not None:
            n_batch_, = lengths.size()
            aeq(n_batch, n_batch_)

    def forward(self, src, lengths=None, encoder_state=None):
        """
        Args:
            src (:obj:`LongTensor`):
               padded sequences of sparse indices `[src_len x batch x nfeat]`
            lengths (:obj:`LongTensor`): length of each sequence `[batch]`
            encoder_state (rnn-class specific):
               initial encoder_state state.

        Returns:
            (tuple of :obj:`FloatTensor`, :obj:`FloatTensor`):
                * final encoder state, used to initialize decoder
                * memory bank for attention, `[src_len x batch x hidden]`
        """
        raise NotImplementedError


class MeanEncoder(EncoderBase):
    """A trivial non-recurrent encoder. Simply applies mean pooling.

    Args:
       num_layers (int): number of replicated layers
       embeddings (:obj:`onmt.modules.Embeddings`): embedding module to use
    """
    def __init__(self, num_layers, embeddings):
        super(MeanEncoder, self).__init__()
        self.num_layers = num_layers
        self.embeddings = embeddings

    def forward(self, src, lengths=None, encoder_state=None):
        "See :obj:`EncoderBase.forward()`"
        self._check_args(src, lengths, encoder_state)

        emb = self.embeddings(src)
        s_len, batch, emb_dim = emb.size()
        mean = emb.mean(0).expand(self.num_layers, batch, emb_dim)
        memory_bank = emb
        encoder_final = (mean, mean)
        return encoder_final, memory_bank


class RNNEncoder(EncoderBase):
    """ A generic recurrent neural network encoder.

    Args:
       rnn_type (:obj:`str`):
          style of recurrent unit to use, one of [RNN, LSTM, GRU, SRU]
       bidirectional (bool) : use a bidirectional RNN
       num_layers (int) : number of stacked layers
       hidden_size (int) : hidden size of each layer
       dropout (float) : dropout value for :obj:`nn.Dropout`
       embeddings (:obj:`onmt.modules.Embeddings`): embedding module to use
    """
    def __init__(self, rnn_type, bidirectional, num_layers,
                 hidden_size, dropout=0.0, embeddings=None,
                 use_bridge=False):
        super(RNNEncoder, self).__init__()
        assert embeddings is not None

        num_directions = 2 if bidirectional else 1
        assert hidden_size % num_directions == 0
        hidden_size = hidden_size // num_directions
        self.embeddings = embeddings

        self.rnn, self.no_pack_padded_seq = \
            rnn_factory(rnn_type,
                        input_size=embeddings.embedding_size,
                        hidden_size=hidden_size,
                        num_layers=num_layers,
                        dropout=dropout,
                        bidirectional=bidirectional)

        # Initialize the bridge layer
        self.use_bridge = use_bridge
        if self.use_bridge:
            self._initialize_bridge(rnn_type,
                                    hidden_size,
                                    num_layers)

    def forward(self, src, lengths=None, encoder_state=None):
        "See :obj:`EncoderBase.forward()`"
        self._check_args(src, lengths, encoder_state)

        emb = self.embeddings(src)
        s_len, batch, emb_dim = emb.size()
        
#         print("Model line:142, emb size", emb.size())

        packed_emb = emb
        if lengths is not None and not self.no_pack_padded_seq:
            # Lengths data is wrapped inside a Variable.
            lengths = lengths.view(-1).tolist()
            packed_emb = pack(emb, lengths)

        memory_bank, encoder_final = self.rnn(packed_emb, encoder_state)

        if lengths is not None and not self.no_pack_padded_seq:
            memory_bank = unpack(memory_bank)[0]

        if self.use_bridge:
            encoder_final = self._bridge(encoder_final)
        return encoder_final, memory_bank

    def _initialize_bridge(self, rnn_type,
                           hidden_size,
                           num_layers):

        # LSTM has hidden and cell state, other only one
        number_of_states = 2 if rnn_type == "LSTM" else 1
        # Total number of states
        self.total_hidden_dim = hidden_size * num_layers

        # Build a linear layer for each
        self.bridge = nn.ModuleList([nn.Linear(self.total_hidden_dim,
                                               self.total_hidden_dim,
                                               bias=True)
                                     for i in range(number_of_states)])

    def _bridge(self, hidden):
        """
        Forward hidden state through bridge
        """
        def bottle_hidden(linear, states):
            """
            Transform from 3D to 2D, apply linear and return initial size
            """
            size = states.size()
            result = linear(states.view(-1, self.total_hidden_dim))
            return F.relu(result).view(size)

        if isinstance(hidden, tuple):  # LSTM
            outs = tuple([bottle_hidden(layer, hidden[ix])
                          for ix, layer in enumerate(self.bridge)])
        else:
            outs = bottle_hidden(self.bridge[0], hidden)
        return outs

class ContextEncoder(EncoderBase):
    """ Context encoder for hierarchical seq2seq

    Args:
       rnn_type (:obj:`str`):
          style of recurrent unit to use, one of [RNN, LSTM, GRU, SRU]
       bidirectional (bool) : use a bidirectional RNN
       num_layers (int) : number of stacked layers
       hidden_size (int) : hidden size of each layer
       dropout (float) : dropout value for :obj:`nn.Dropout`
       input_size (int): size of input
    """
    def __init__(self, rnn_type, bidirectional, num_layers,
                 hidden_size, dropout=0.0, input_size=None,
                 use_bridge=False):
        super(ContextEncoder, self).__init__()
        assert input_size is not None

        num_directions = 2 if bidirectional else 1
        assert hidden_size % num_directions == 0
        hidden_size = hidden_size // num_directions

        self.rnn, self.no_pack_padded_seq = \
            rnn_factory(rnn_type,
                        input_size=input_size,
                        hidden_size=hidden_size,
                        num_layers=num_layers,
                        dropout=dropout,
                        bidirectional=bidirectional)

        # Initialize the bridge layer
        self.use_bridge = use_bridge
        if self.use_bridge:
            self._initialize_bridge(rnn_type,
                                    hidden_size,
                                    num_layers)

    def forward(self, src, lengths=None, encoder_state=None):
        "See :obj:`EncoderBase.forward()`"
        self._check_args(src, lengths, encoder_state)

        s_len, batch, emb_dim = src.size()

        packed_emb = src
        if lengths is not None and not self.no_pack_padded_seq:
            # Lengths data is wrapped inside a Variable.
            lengths = lengths.view(-1).tolist()
            packed_emb = pack(src, lengths)

        memory_bank, encoder_final = self.rnn(packed_emb, encoder_state)
#         print("model line:243 context encoder output mem bank", memory_bank.size())
#         print("model line:244 context encoder output encode final", encoder_final)

        if lengths is not None and not self.no_pack_padded_seq:
            memory_bank = unpack(memory_bank)[0]

        if self.use_bridge:
            encoder_final = self._bridge(encoder_final)
        return encoder_final, memory_bank

    def _initialize_bridge(self, rnn_type,
                           hidden_size,
                           num_layers):

        # LSTM has hidden and cell state, other only one
        number_of_states = 2 if rnn_type == "LSTM" else 1
        # Total number of states
        self.total_hidden_dim = hidden_size * num_layers

        # Build a linear layer for each
        self.bridge = nn.ModuleList([nn.Linear(self.total_hidden_dim,
                                               self.total_hidden_dim,
                                               bias=True)
                                     for i in range(number_of_states)])

    def _bridge(self, hidden):
        """
        Forward hidden state through bridge
        """
        def bottle_hidden(linear, states):
            """
            Transform from 3D to 2D, apply linear and return initial size
            """
            size = states.size()
            result = linear(states.view(-1, self.total_hidden_dim))
            return F.relu(result).view(size)

        if isinstance(hidden, tuple):  # LSTM
            outs = tuple([bottle_hidden(layer, hidden[ix])
                          for ix, layer in enumerate(self.bridge)])
        else:
            outs = bottle_hidden(self.bridge[0], hidden)
        return outs    
    

class RNNDecoderBase(nn.Module):
    """
    Base recurrent attention-based decoder class.
    Specifies the interface used by different decoder types
    and required by :obj:`onmt.Models.NMTModel`.


    .. mermaid::

       graph BT
          A[Input]
          subgraph RNN
             C[Pos 1]
             D[Pos 2]
             E[Pos N]
          end
          G[Decoder State]
          H[Decoder State]
          I[Outputs]
          F[Memory_Bank]
          A--emb-->C
          A--emb-->D
          A--emb-->E
          H-->C
          C-- attn --- F
          D-- attn --- F
          E-- attn --- F
          C-->I
          D-->I
          E-->I
          E-->G
          F---I

    Args:
       rnn_type (:obj:`str`):
          style of recurrent unit to use, one of [RNN, LSTM, GRU, SRU]
       bidirectional_encoder (bool) : use with a bidirectional encoder
       num_layers (int) : number of stacked layers
       hidden_size (int) : hidden size of each layer
       attn_type (str) : see :obj:`onmt.modules.GlobalAttention`
       coverage_attn (str): see :obj:`onmt.modules.GlobalAttention`
       context_gate (str): see :obj:`onmt.modules.ContextGate`
       copy_attn (bool): setup a separate copy attention mechanism
       dropout (float) : dropout value for :obj:`nn.Dropout`
       embeddings (:obj:`onmt.modules.Embeddings`): embedding module to use
    """
    def __init__(self, rnn_type, bidirectional_encoder, num_layers,
                 hidden_size, attn_type="general",
                 coverage_attn=False, context_gate=None,
                 copy_attn=False, dropout=0.0, embeddings=None,
                 reuse_copy_attn=False,
                 model_type=None ):
        super(RNNDecoderBase, self).__init__()

        # Basic attributes.
        self.decoder_type = 'rnn'
        self.bidirectional_encoder = bidirectional_encoder
        self.num_layers = num_layers
        self.hidden_size = hidden_size
        self.embeddings = embeddings
        self.dropout = nn.Dropout(dropout)

        # Build the RNN.
        self.rnn = self._build_rnn(rnn_type,
                                   input_size=self._input_size,
                                   hidden_size=hidden_size,
                                   num_layers=num_layers,
                                   dropout=dropout)

        # Set up the context gate.
        self.context_gate = None
        if context_gate is not None:
            self.context_gate = onmt.modules.context_gate_factory(
                context_gate, self._input_size,
                hidden_size, hidden_size, hidden_size
            )

        # Set up the standard attention.
        self._coverage = coverage_attn
        # hierarchical model
        if model_type == "hierarchical_text":
            self.attn = onmt.modules.HierarchicalAttention(
                hidden_size, coverage=coverage_attn,
                attn_type=attn_type
            )            
            print("model line:373 hierarchical attn")
        else:
            self.attn = onmt.modules.GlobalAttention(
                hidden_size, coverage=coverage_attn,
                attn_type=attn_type
            )

        # Set up a separated copy attention layer, if needed.
        self._copy = False
        if copy_attn and not reuse_copy_attn:
            self.copy_attn = onmt.modules.GlobalAttention(
                hidden_size, attn_type=attn_type
            )
        if copy_attn:
            self._copy = True
        self._reuse_copy_attn = reuse_copy_attn

    def forward(self, tgt, memory_bank, state, memory_lengths=None, idf_weights=None):
        """
        Args:
            tgt (`LongTensor`): sequences of padded tokens
                                `[tgt_len x batch x nfeats]`.
            memory_bank (`FloatTensor`): vectors from the encoder
                 `[src_len x batch x hidden]`.
            state (:obj:`onmt.Models.DecoderState`):
                 decoder state object to initialize the decoder
            memory_lengths (`LongTensor`): the padded source lengths
                `[batch]`.
            # 18.07.05 by thkim
            idf_weights : idf values, multiply it to attn weight
            
        Returns:
            (`FloatTensor`,:obj:`onmt.Models.DecoderState`,`FloatTensor`):
                * decoder_outputs: output from the decoder (after attn)
                         `[tgt_len x batch x hidden]`.
                * decoder_state: final hidden state from the decoder
                * attns: distribution over src at each tgt
                        `[tgt_len x batch x src_len]`.
        """
        # Check
        assert isinstance(state, RNNDecoderState)
        tgt_len, tgt_batch, _ = tgt.size()
        _, memory_batch, _ = memory_bank.size()
        aeq(tgt_batch, memory_batch)
        # END

        # Run the forward pass of the RNN.
        decoder_final, decoder_outputs, attns = self._run_forward_pass(
            tgt, memory_bank, state, memory_lengths=memory_lengths, idf_weights=idf_weights)

        # Update the state with the result.
        final_output = decoder_outputs[-1]
        coverage = None
        if "coverage" in attns:
            coverage = attns["coverage"][-1].unsqueeze(0)
        state.update_state(decoder_final, final_output.unsqueeze(0), coverage)

        # Concatenates sequence of tensors along a new dimension.
        decoder_outputs = torch.stack(decoder_outputs)
        for k in attns:
            attns[k] = torch.stack(attns[k])

        return decoder_outputs, state, attns
   

    def init_decoder_state(self, src, memory_bank, encoder_final):
        def _fix_enc_hidden(h):
            # The encoder hidden is  (layers*directions) x batch x dim.
            # We need to convert it to layers x batch x (directions*dim).
            if self.bidirectional_encoder:
                h = torch.cat([h[0:h.size(0):2], h[1:h.size(0):2]], 2)
            return h

        if isinstance(encoder_final, tuple):  # LSTM
            return RNNDecoderState(self.hidden_size,
                                   tuple([_fix_enc_hidden(enc_hid)
                                         for enc_hid in encoder_final]))
        else:  # GRU
            return RNNDecoderState(self.hidden_size,
                                   _fix_enc_hidden(encoder_final))
        



class StdRNNDecoder(RNNDecoderBase):
    """
    Standard fully batched RNN decoder with attention.
    Faster implementation, uses CuDNN for implementation.
    See :obj:`RNNDecoderBase` for options.


    Based around the approach from
    "Neural Machine Translation By Jointly Learning To Align and Translate"
    :cite:`Bahdanau2015`


    Implemented without input_feeding and currently with no `coverage_attn`
    or `copy_attn` support.
    """
    def _run_forward_pass(self, tgt, memory_bank, state, memory_lengths=None, idf_weights=None):
        """
        Private helper for running the specific RNN forward pass.
        Must be overriden by all subclasses.
        Args:
            tgt (LongTensor): a sequence of input tokens tensors
                                 [len x batch x nfeats].
            memory_bank (FloatTensor): output(tensor sequence) from the encoder
                        RNN of size (src_len x batch x hidden_size).
            state (FloatTensor): hidden state from the encoder RNN for
                                 initializing the decoder.
            memory_lengths (LongTensor): the source memory_bank lengths.
        Returns:
            decoder_final (Variable): final hidden state from the decoder.
            decoder_outputs ([FloatTensor]): an array of output of every time
                                     step from the decoder.
            attns (dict of (str, [FloatTensor]): a dictionary of different
                            type of attention Tensor array of every time
                            step from the decoder.
        """
        assert not self._copy  # TODO, no support yet.
        assert not self._coverage  # TODO, no support yet.

        # Initialize local and return variables.
        attns = {}
        emb = self.embeddings(tgt)

        # Run the forward pass of the RNN.
        if isinstance(self.rnn, nn.GRU):
            rnn_output, decoder_final = self.rnn(emb, state.hidden[0])
        else:
            rnn_output, decoder_final = self.rnn(emb, state.hidden)

        # Check
        tgt_len, tgt_batch, _ = tgt.size()
        output_len, output_batch, _ = rnn_output.size()
        aeq(tgt_len, output_len)
        aeq(tgt_batch, output_batch)
        # END

        # Calculate the attention.
        decoder_outputs, p_attn = self.attn(
            rnn_output.transpose(0, 1).contiguous(),
            memory_bank.transpose(0, 1),
            memory_lengths=memory_lengths
        )
        attns["std"] = p_attn

        # Calculate the context gate.
        if self.context_gate is not None:
            decoder_outputs = self.context_gate(
                emb.view(-1, emb.size(2)),
                rnn_output.view(-1, rnn_output.size(2)),
                decoder_outputs.view(-1, decoder_outputs.size(2))
            )
            decoder_outputs = \
                decoder_outputs.view(tgt_len, tgt_batch, self.hidden_size)

        decoder_outputs = self.dropout(decoder_outputs)
        return decoder_final, decoder_outputs, attns

    def _build_rnn(self, rnn_type, **kwargs):
        rnn, _ = rnn_factory(rnn_type, **kwargs)
        return rnn

    @property
    def _input_size(self):
        """
        Private helper returning the number of expected features.
        """
        return self.embeddings.embedding_size


class InputFeedRNNDecoder(RNNDecoderBase):
    """
    Input feeding based decoder. See :obj:`RNNDecoderBase` for options.

    Based around the input feeding approach from
    "Effective Approaches to Attention-based Neural Machine Translation"
    :cite:`Luong2015`


    .. mermaid::

       graph BT
          A[Input n-1]
          AB[Input n]
          subgraph RNN
            E[Pos n-1]
            F[Pos n]
            E --> F
          end
          G[Encoder]
          H[Memory_Bank n-1]
          A --> E
          AB --> F
          E --> H
          G --> H
    """

    def _run_forward_pass(self, tgt, memory_bank, state, memory_lengths=None, idf_weights=None):
        """
        See StdRNNDecoder._run_forward_pass() for description
        of arguments and return values.
        """
        # Additional args check.
        input_feed = state.input_feed.squeeze(0)
        input_feed_batch, _ = input_feed.size()
        tgt_len, tgt_batch, _ = tgt.size()
        aeq(tgt_batch, input_feed_batch)
        # END Additional args check.

        # Initialize local and return variables.
        decoder_outputs = []
        attns = {"std": []}
        if self._copy:
            attns["copy"] = []
        if self._coverage:
            attns["coverage"] = []

        # for intra-temporal attention, init attn history per every batches
#         self.attn.init_attn_outputs()
        # for intra-decoder attention, init decoder history per every batches
#         self.attn.init_decoder_outputs()
        
#         print("model line:486, mb", memory_bank)
#         print(tgt[0])
#         input("model line:497")

        emb = self.embeddings(tgt)
        assert emb.dim() == 3  # len x batch x embedding_dim

        hidden = state.hidden
        coverage = state.coverage.squeeze(0) \
            if state.coverage is not None else None

        # Input feed concatenates hidden state with
        # input at every time step.
        for i, emb_t in enumerate(emb.split(1)):

            emb_t = emb_t.squeeze(0)
            decoder_input = torch.cat([emb_t, input_feed], 1)
            rnn_output, hidden = self.rnn(decoder_input, hidden)
#             print("model line 508 before attn")
            #print("model line:815 decoder rnn output", rnn_output) # batch * hidden
            decoder_output, p_attn = self.attn(
                rnn_output,
                memory_bank.transpose(0, 1),
                memory_lengths=memory_lengths,
                emb_weight=self.embeddings.word_lut.weight,
                idf_weights = idf_weights
            ) # for sharing decoder weight
#             print("model line 513 after attn")
            if self.context_gate is not None:
                # TODO: context gate should be employed
                # instead of second RNN transform.
                decoder_output = self.context_gate(
                    decoder_input, rnn_output, decoder_output
                )
            decoder_output = self.dropout(decoder_output)
            input_feed = decoder_output

            decoder_outputs += [decoder_output]
#             print("model line:529 dec out", decoder_output.size())
            attns["std"] += [p_attn]
#             print("model line:530 p_attn", p_attn)

            # Update the coverage attention.
            if self._coverage:
                coverage = coverage + p_attn \
                    if coverage is not None else p_attn
                attns["coverage"] += [coverage]

            # Run the forward pass of the copy attention layer.
            # TODO 이게 왜 있는지 알아봐야함
#             if self._copy and not self._reuse_copy_attn:
#                 _, copy_attn = self.copy_attn(decoder_output,
#                                               memory_bank.transpose(0, 1))
#                 attns["copy"] += [copy_attn]
            if self._copy:
                attns["copy"] = attns["std"]
#         print("model line:509 attns", attns)
        # Return result.
        return hidden, decoder_outputs, attns
    
    def _build_rnn(self, rnn_type, input_size,
                   hidden_size, num_layers, dropout):
        assert not rnn_type == "SRU", "SRU doesn't support input feed! " \
                "Please set -input_feed 0!"
        if rnn_type == "LSTM":
            stacked_cell = onmt.modules.StackedLSTM
        else:
            stacked_cell = onmt.modules.StackedGRU
        return stacked_cell(num_layers, input_size,
                            hidden_size, dropout)

    # initiating attn history information (for intra-temporal, intra decoder attn)
    def init_attn_history(self):
        # for intra-temporal attention, init attn history per every batches
        self.attn.init_attn_outputs()
        # for intra-decoder attention, init decoder history per every batches
        self.attn.init_decoder_outputs()
#         print("Model line:371, attn history called")    
    
    @property
    def _input_size(self):
        """
        Using input feed by concatenating input with attention vectors.
        """
        return self.embeddings.embedding_size + self.hidden_size    
    
class HierarchicalInputFeedRNNDecoder(RNNDecoderBase):
    """
    Input feeding based decoder. See :obj:`RNNDecoderBase` for options.

    Based around the input feeding approach from
    "Effective Approaches to Attention-based Neural Machine Translation"
    :cite:`Luong2015`


    .. mermaid::

       graph BT
          A[Input n-1]
          AB[Input n]
          subgraph RNN
            E[Pos n-1]
            F[Pos n]
            E --> F
          end
          G[Encoder]
          H[Memory_Bank n-1]
          A --> E
          AB --> F
          E --> H
          G --> H
    """
    def forward(self, tgt, sentence_memory_bank, context_memory_bank, state, sentence_memory_lengths, context_memory_lengths, context_mask, idf_weights=None):
        """
        Args:
            # 18.07.24 thkim
            tgt (`LongTensor`): sequences of padded tokens
                                `[tgt_len x batch x nfeats]`.
            sentence_memory_bank (`FloatTensor`): vectors from the each sentence encoder res
                 `[src_len x batch x hidden]`.
            context_memory_bank (`FloatTensor`): vectors from the context encoder res
                 `[src_len x batch x hidden]`.
            state (:obj:`onmt.Models.DecoderState`):
                 decoder state object to initialize the decoder
            context_memory_lengths (`LongTensor`): the padded context lengths
                `[batch]`.
            # 18.07.05 by thkim
            idf_weights : idf values, multiply it to attn weight
            
        Returns:
            (`FloatTensor`,:obj:`onmt.Models.DecoderState`,`FloatTensor`):
                * decoder_outputs: output from the decoder (after attn)
                         `[tgt_len x batch x hidden]`.
                * decoder_state: final hidden state from the decoder
                * attns: distribution over src at each tgt
                        `[tgt_len x batch x src_len]`.
        """
        # Check
        assert isinstance(state, RNNDecoderState)
        tgt_len, tgt_batch, _ = tgt.size()
        _, memory_batch, _ = context_memory_bank.size()
        aeq(tgt_batch, memory_batch)
        # END
        
        # Run the forward pass of the RNN.
        decoder_final, decoder_outputs, attns, context_attns = self._run_forward_pass(
            tgt, sentence_memory_bank, context_memory_bank, state, sentence_memory_lengths, context_memory_lengths, context_mask, idf_weights=idf_weights)

        # Update the state with the result.
        final_output = decoder_outputs[-1]
        coverage = None
        if "coverage" in attns:
            coverage = attns["coverage"][-1].unsqueeze(0)
        state.update_state(decoder_final, final_output.unsqueeze(0), coverage)

        # Concatenates sequence of tensors along a new dimension.
        decoder_outputs = torch.stack(decoder_outputs)
        for k in attns:
            attns[k] = torch.stack(attns[k])

        return decoder_outputs, state, attns, context_attns    
    

    def _run_forward_pass(self, tgt, sentence_memory_bank, context_memory_bank, state, sentence_memory_lengths, context_memory_lengths, context_mask, idf_weights=None):
        """
        See StdRNNDecoder._run_forward_pass() for description
        of arguments and return values.
        """
        # Additional args check.
        input_feed = state.input_feed.squeeze(0)
        input_feed_batch, _ = input_feed.size()
        tgt_len, tgt_batch, _ = tgt.size()
        aeq(tgt_batch, input_feed_batch)
        # END Additional args check.

        # Initialize local and return variables.
        decoder_outputs = []
        attns = {"std": []}
        context_attns = {"std": []}
        if self._copy:
            attns["copy"] = []
            context_attns["copy"]  = []
        if self._coverage:
            attns["coverage"] = []
            context_attns["coverage"] = []
            
#         print("model line 785 sent m bank", sentence_memory_bank)
#         print("model line 785 context m bank", context_memory_bank)
#         print("model line 785 sentence_memory_lengths", sentence_memory_lengths)
#         print("model line 785 context_memory_lengths", context_memory_lengths)
            
        # for intra-temporal attention, init attn history per every batches
#         self.attn.init_attn_outputs()
        # for intra-decoder attention, init decoder history per every batches
#         self.attn.init_decoder_outputs()
        
#         print("model line:486, mb", memory_bank)
#         print(tgt[0])
#         input("model line:497")

        emb = self.embeddings(tgt)
        assert emb.dim() == 3  # len x batch x embedding_dim

        hidden = state.hidden
        coverage = state.coverage.squeeze(0) \
            if state.coverage is not None else None

        # Input feed concatenates hidden state with
        # input at every time step.
        for i, emb_t in enumerate(emb.split(1)):

            emb_t = emb_t.squeeze(0)
            decoder_input = torch.cat([emb_t, input_feed], 1)

#             print("model line:813 decoder hidden", hidden)
#             print("model line:813 decoder input", decoder_input)
#             print("model line:813 decoder input", input_feed)
            rnn_output, hidden = self.rnn(decoder_input, hidden)
#             print("model line 508 before attn")
#             print("model line:815 decoder rnn output", rnn_output) # batch * hidden


            decoder_output, p_attn, context_attn = self.attn(
                rnn_output,
                sentence_memory_bank.transpose(0, 1),
                context_memory_bank.transpose(0, 1),
                sentence_memory_lengths,
                context_memory_lengths,
                context_mask,
                emb_weight=self.embeddings.word_lut.weight,
                idf_weights = idf_weights
            ) # for sharing decoder weight
#             print("model line 513 after attn")

            if self.context_gate is not None:
                # TODO: context gate should be employed
                # instead of second RNN transform.
                decoder_output = self.context_gate(
                    decoder_input, rnn_output, decoder_output
                )
            decoder_output = self.dropout(decoder_output)
            input_feed = decoder_output

            decoder_outputs += [decoder_output]
#             print("model line:529 dec out", decoder_output.size())
            attns["std"] += [p_attn]
            context_attns["std"] += [context_attn]
#             print("model line:530 p_attn", p_attn)

            # Update the coverage attention.
            if self._coverage:
                coverage = coverage + p_attn \
                    if coverage is not None else p_attn
                attns["coverage"] += [coverage]
                context_attns["coverage"] += [coverage]

            # Run the forward pass of the copy attention layer.
            # TODO 이게 왜 있는지 알아봐야함
#             if self._copy and not self._reuse_copy_attn:
#                 _, copy_attn = self.copy_attn(decoder_output,
#                                               memory_bank.transpose(0, 1))
#                 attns["copy"] += [copy_attn]
            if self._copy:
                attns["copy"] = attns["std"]
                context_attns["copy"] = context_attns["std"]
#         print("model line:509 attns", attns)
        # Return result.
        return hidden, decoder_outputs, attns, context_attns  

    def _build_rnn(self, rnn_type, input_size,
                   hidden_size, num_layers, dropout):
        assert not rnn_type == "SRU", "SRU doesn't support input feed! " \
                "Please set -input_feed 0!"
        if rnn_type == "LSTM":
            stacked_cell = onmt.modules.StackedLSTM
        else:
            stacked_cell = onmt.modules.StackedGRU
        return stacked_cell(num_layers, input_size,
                            hidden_size, dropout)

    # initiating attn history information (for intra-temporal, intra decoder attn)
    def init_attn_history(self):
        # for intra-temporal attention, init attn history per every batches
        self.attn.init_attn_outputs()
        # for intra-decoder attention, init decoder history per every batches
        self.attn.init_decoder_outputs()
#         print("Model line:371, attn history called")    
    
    @property
    def _input_size(self):
        """
        Using input feed by concatenating input with attention vectors.
        """
        return self.embeddings.embedding_size + self.hidden_size


class NMTModel(nn.Module):
    """
    Core trainable object in OpenNMT. Implements a trainable interface
    for a simple, generic encoder + decoder model.

    Args:
      encoder (:obj:`EncoderBase`): an encoder object
      decoder (:obj:`RNNDecoderBase`): a decoder object
      multi<gpu (bool): setup for multigpu support
    """
    def __init__(self, encoder, decoder, multigpu=False):
        self.multigpu = multigpu
        super(NMTModel, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

    def forward(self, src, tgt, lengths, dec_state=None, batch=None):
        """Forward propagate a `src` and `tgt` pair for training.
            Possible initialized with a beginning decoder state.

            Args:
                src (:obj:`Tensor`):
                    a source sequence passed to encoder.
                    typically for inputs this will be a padded :obj:`LongTensor`
                    of size `[len x batch x features]`. however, may be an
                    image or other generic input depending on encoder.
                tgt (:obj:`LongTensor`):
                     a target sequence of size `[tgt_len x batch]`.
                lengths(:obj:`LongTensor`): the src lengths, pre-padding `[batch]`.
                dec_state (:obj:`DecoderState`, optional): initial decoder state
            Returns:
                (:obj:`FloatTensor`, `dict`, :obj:`onmt.Models.DecoderState`):

                     * decoder output `[tgt_len x batch x hidden]`
                     * dictionary attention dists of `[tgt_len x batch x src_len]`
                     * final decoder state
        """
#         print("model line:602", self.obj_f)
        tgt = tgt[:-1]  # exclude last target from inputs
       
        
        enc_final, memory_bank = self.encoder(src, lengths)
        enc_state = \
            self.decoder.init_decoder_state(src, memory_bank, enc_final)
        self.decoder.init_attn_history() # init attn history in decoder for new attention
        
        decoder_outputs, dec_state, attns = \
            self.decoder(tgt, memory_bank,
                         enc_state if dec_state is None
                         else dec_state,
                         memory_lengths=lengths)
        if self.multigpu:
            # Not yet supported on multi-gpu
            dec_state = None
            attns = None            
            
        return decoder_outputs, attns, dec_state

    
    def sample(self, src, tgt, lengths, dec_states=None, batch=None, mode="sample", eos_index=3):
        """Forward propagate a `src` and `tgt` pair for training.
        Possible initialized with a beginning decoder state.

        Args:
            src (:obj:`Tensor`):
                a source sequence passed to encoder.
                typically for inputs this will be a padded :obj:`LongTensor`
                of size `[len x batch x features]`. however, may be an
                image or other generic input depending on encoder.
            tgt (:obj:`LongTensor`):
                 a target sequence of size `[tgt_len x batch]`.
            lengths(:obj:`LongTensor`): the src lengths, pre-padding `[batch]`.
            dec_states (:obj:`DecoderState`, optional): initial decoder state
        Returns:
            (:obj:`FloatTensor`, `dict`, :obj:`onmt.Models.DecoderState`):

                 * decoder output `[tgt_len x batch x hidden]`
                 * dictionary attention dists of `[tgt_len x batch x src_len]`
                 * final decoder state
        """
        
#         print("model line:602", self.obj_f)
        tgt = tgt[:-1]  # exclude last target from inputs

        enc_final, memory_bank = self.encoder(src, lengths)
        dec_states = \
            self.decoder.init_decoder_state(src, memory_bank, enc_final)
       
#         print("model line 662 enc_state", enc_final)
#         print("model line 663 enc_state hidden", enc_state.hidden)
#         input()
            
        self.decoder.init_attn_history() # init attn history in decoder for new attention
        
        assert self.obj_f == "rl" or self.obj_f == "hybrid"
        assert batch != None
        def _unbottle(v, batch_size):
            return v.view(-1, batch_size, v.size(1))
            
        # Initialize local and return variables.
        decoder_outputs = []
        probs = []
        out_indices = []
        attns = {"std": []}
        if self.decoder._copy:
            attns["copy"] = []
        if self.decoder._coverage:
            attns["coverage"] = []
                     
#         print("model line:622, tgt.size", tgt.size(0))
        for i in range(tgt.size(0)):
            # initial bos tokens
            if i == 0:
                inp = tgt[0].unsqueeze(0)
#                 print("model line:679 inp", inp.view(1,-1))
#                 print("model line:689 inpu.req", inp.requires_grad)
            # test code
            else:
#                 print("model line:682 inp", i, out_indices[-1])            
#                 input()
                inp = Variable(out_indices[-1], requires_grad=False).cuda().unsqueeze(0).unsqueeze(2)
                



            # Turn any copied words to UNKs
            # 0 is unk
            if self.decoder._copy or self.decoder.copy_attn:
                inp = inp.masked_fill(
                    inp.gt(len(batch.dataset.fields['tgt'].vocab) - 1), 0)
#             inp = inp.unsqueeze(2)             
#             print("model line:682 inp", i, inp)

            # Run one step.
            dec_out, dec_states, attn = self.decoder(
                inp, memory_bank, dec_states, memory_lengths=lengths)
            dec_out = dec_out.squeeze(0)
            # dec_out: beam x rnn_size                    

            # (b) Compute a vector of batch x beam word scores.
            if not self.decoder._copy and not self.decoder.copy_attn:
                out = self.generator.forward(dec_out).data
                out = unbottle(out)
                # beam x tgt_vocab
                beam_attn = unbottle(attn["std"])
            else:
                out = self.generator.forward(dec_out,
                                                   attn["copy"].squeeze(0),
                                                   batch.src_map)
                # batch x (tgt_vocab + extra_vocab)
#                 print("model line:714 out dec_out", dec_out) # variable
#                 print("model line:714 out prob", out) # variable
#                 print("model line:729 out data prob", out) # batch * vocab size
                out = batch.dataset.collapse_copy_scores(
                    _unbottle(out.data, len(batch)),
                    batch, batch.dataset.fields["tgt"].vocab, batch.dataset.src_vocabs)
                # batch x tgt_vocab
#                 out_data = out_data.log()
                out = out.log().squeeze(0) # batch_size * (tgt_vocab + ext)
                
#             print("model line:729 out mode", mode)
#             print("model line:729 out data prob", out) # batch * vocab size
#             input()

            if mode == "greedy":
#                 _, index = torch.max(out.data,1)
                _, index = torch.max(out,1)
#                 print("model line 734, index", index)
#                 print("model line 735, out", out)
#                 prob = out.gather(1, Variable(index.unsqueeze(1), requires_grad=False)) # batchsize * 1
                # prob : 1*batch size
                # index : 1*batcch size
            elif mode == "sample": 
#                 print("model line:734 multi", torch.exp(out.squeeze(0)))
#                 input()
#                 index = torch.multinomial(torch.exp(out.data.squeeze(0)),1) # batchsize*1
                index = torch.multinomial(torch.exp(out),1) # batchsize*1
#                 print("model line 734, index", index)
#                 print("model line 735, out", out)
#                 prob = out.gather(1, Variable(index, requires_grad=False)) # batchsize * 1
    
            index = index.view(-1)
        
            # control intermediate termination 
            # eos index is 3
            # padding index is 1
            if i == 0:
                unfinished = index != eos_index
            else:
                unfinished = unfinished * ( out_indices[-1] != eos_index )
            if unfinished.sum() == 0:
                break
                
            if len(out_indices) > 0:
#                 print("Model line 766: index", index)
                index = index * unfinished.type_as(index) + (unfinished == 0).type_as(index) # pad 1
#                 print("Model line 766: index", index)
#                 print("Model line 767: unfinished", unfinished)
            
#             probs += [prob.view(-1)]
            out_indices += [index]

#             print("model line:726 out_indices")
#             for index in out_indices:
#                 print(index.view(1,-1))
            
                    
            # update history
#             print("model line:679 dec out", dec_out.size()) # batch * hidden size
            decoder_outputs += [dec_out]
#             print("model line:677 attns", attn["std"][0])
            attns["std"] += [attn["std"][0]]
            # Update the coverage attention.
            if self.decoder._coverage:
                coverage = coverage + p_attn \
                    if coverage is not None else p_attn
                attns["coverage"] += attn["coverage"][0]

            # Run the forward pass of the copy attention layer.
            # TODO
#             if self.decoder._copy and not self.decoder._reuse_copy_attn:
#                 _, copy_attn = self.decoder.copy_attn(dec_out,
#                                               memory_bank.transpose(0, 1))
#                 attns["copy"] += [copy_attn]
            if self.decoder._copy:
                attns["copy"] = attns["std"] 
                
        # Update the state with the result.
        final_output = decoder_outputs[-1]
        coverage = None
        if "coverage" in attns:
            coverage = attns["coverage"][-1].unsqueeze(0)
        dec_states.update_state(dec_out, final_output.unsqueeze(0), coverage)

        # Concatenates sequence of tensors along a new dimension.
        decoder_outputs = torch.stack(decoder_outputs)
        for k in attns:
            attns[k] = torch.stack(attns[k])              

        # pad eos if not finished 3 is eos token
        # remove
#         if unfinished.sum() != 0:
#             unfinished = unfinished * ( out_indices[-1] != eos_index )
#             index = eos_index * unfinished.type_as(index) +  (unfinished == 0).type_as(index) # pad 1
#             out_indices += [index]    
    #         probs = torch.stack(probs)
        out_indices = torch.stack(out_indices)
#         print("model line 770 probx, out_indices", probs.size(), out_indices.size()) # tgt_len * batch size
#         input("model line:771")
    
#         return decoder_outputs, attns, dec_states, probs, out_indices
        return decoder_outputs, attns, dec_states, out_indices

class HierarchicalModel(nn.Module):
    """
    Core trainable object in OpenNMT. Implements a trainable interface
    for a simple, generic encoder + decoder model.

    Args:
      encoder (:obj:`EncoderBase`): an encoder object
      decoder (:obj:`RNNDecoderBase`): a decoder object
      multi<gpu (bool): setup for multigpu support
    """
    def __init__(self, context_encoder, sent_encoder, decoder, multigpu=False):
        self.multigpu = multigpu
        super(HierarchicalModel, self).__init__()
        self.context_encoder = context_encoder
        self.sent_encoder = sent_encoder
        self.decoder = decoder
        
    def hierarchical_encode(self, src, lengths, batch):
        max_context_length = torch.max(batch.context_lengthes)
#         print("Model line 1102, context_lengthes", max_context_length)
#         print("Model line 1103, ssrc size", src.size()) # max_length * batch * 1
#         print("Model line 1103, batchsize",len(batch))
#         input("Model line 1101")

        # for test
        src_vocab = batch.dataset.fields['src'].vocab
        
        def get_context(src, context_mask, batch_size, index):
            # arg
            # src : length * batch size
            submask = context_mask == index
#             print("Model line:1110 submask", submask) # src_len * batch size
#             print("Model line:1118 context_mask", context_mask.size()) # src_len * batch size

            sub_context = []
            sub_context_len = torch.sum(submask, 0).long()
            max_sub_context_len = torch.max(sub_context_len)
#             print("Model line:1110 submask 0", torch.sum(submask, 0).view(1,-1)) # batch size
#             print("Model line:1110 submask 1", torch.sum(submask, 1).view(1,-1)) # src length
#             print("Model line:1117 sum max_sub_context_len", max_sub_context_len) # batch_size
#             print("Model line:1118 sum submask", submask.long().sum())
#             print("Model line:1119 mask select", torch.masked_select(src, submask).view(1,-1)) # sum submask
    
            selected_context = torch.masked_select(src.squeeze(-1).t(), submask.t())
#             print("Model line:1123 selected context len", selected_context.size())
            sub_context = torch.ones(batch_size, max_sub_context_len.data[0]).long()
#             print("Model line:1124 sub_context", sub_context)
            accm = 0
            for i, mask_len in enumerate(sub_context_len): # batch size
                mask_len = mask_len.data[0]
#                 print("Model line:1128, mask_len", mask_len)
                if mask_len == 0:
                    continue
                sub_context[i][:mask_len] = selected_context.data[accm:accm + mask_len]
                accm += mask_len
#                 print(accm)
#             print("Model line:1124 sub_context", sub_context.t()) # batch size * max context length
#             print("Model line:1124 index", index) # batch size * max context length
    
            # test for correctly gathered sub context information
#             for ii in range(batch_size):
#                 for jj in range(max_sub_context_len.data[0]):
#                     print(src_vocab.itos[sub_context[ii][jj]], end = ' ')
#                 print()
#                 for jj in range(max_sub_context_len.data[0]):
#                     print(src_vocab.itos[src.data[jj][ii][0]], end = ' ')
#                 print()                
#                 for jj in range(max_sub_context_len.data[0]):
#                     print(context_mask.data[jj][ii], end = ' ')
#                 print()                
#                 print()
            sub_context = Variable(sub_context.t(), requires_grad=False).cuda() # max context length * batch size
#             input("line:1218")            
            return sub_context.unsqueeze(2), sub_context_len
            
        ####################
        # sentence encoding
        ####################   
    
        # sentence encoder history
        sentence_memory_bank = [0] * len(batch)
        is_empty_sentence_memory_bank = True

        # context encoder input
        context_inputs = [0] * len(batch)
        dummy_context_inputs = None
        
        for idx in range(max_context_length.data[0]):
            sub_context, sub_context_len = get_context(src, batch.context_mask, len(batch), idx)    
            sent_final, sent_memory_bank = self.sent_encoder(sub_context, sub_context_len)
#             print("Model line:1142 seq final", sent_final) 
#             print("Model line:1142 seq sent_memory_bank", sent_memory_bank.size()) # nax_sub_context_len * batch * hidden size
#             print("Model line:1166 seq sub_context_mask", sub_context > 1)
            
            sub_context_mask = sub_context != 1 # max_sub_context_len * batch size * emb size
            selected_sent_context = torch.masked_select(sent_memory_bank.transpose(0,1), sub_context_mask.transpose(0,1)).view(-1, sent_memory_bank.size(2))
#             print("Model line:1171 seq seleted_sent_context", selected_sent_context) 
        
            accm = 0            
            if dummy_context_inputs is None:
                dummy_context_inputs = Variable(torch.zeros((1, sent_memory_bank.size(2))), requires_grad=False).cuda()
#                 print("Model line:1183 dummy_context_inputs", dummy_context_inputs)
            for i, mask_len in enumerate(sub_context_len): # batch size              
                mask_len = mask_len.data[0]
                if mask_len == 0:
                    context_inputs[i] = torch.cat((context_inputs[i], dummy_context_inputs), 0)
                    continue
                
                if is_empty_sentence_memory_bank:
                    sentence_memory_bank[i] = selected_sent_context[accm:accm + mask_len]
                    context_inputs[i] = selected_sent_context[accm + mask_len-1:accm + mask_len]
#                     print("Model line:1183 sentence_memory bank element size", sentence_memory_bank[i].size())
#                     print("Model line:1183 context input size", context_inputs[i].size())
                    
                else:
#                     print("Model line:1196 sentence_memory_bank", sentence_memory_bank[i]) 
#                     print("Model line:1197 sub selected_sent_context", selected_sent_context[accm:accm + mask_len])
                    sentence_memory_bank[i] = torch.cat((sentence_memory_bank[i], selected_sent_context[accm:accm + mask_len]), 0)
                    context_inputs[i] = torch.cat((context_inputs[i], selected_sent_context[accm + mask_len-1].unsqueeze(0)), 0)
#                     try:
#                         pass
#                     except:
#                         print("i", idx)
#                         print("i", i)
#                         print("accm", accm)
#                         print("mask_len", mask_len)
#                         print("sub_context", sub_context) # 
#                         print("sub_context_len", sub_context_len)
                        
#                         print("sent_memory_bank", sent_memory_bank.size())
#                         print("seq seleted_sent_context", selected_sent_context.size()) 
#                         print("0", src_vocab.itos[0])
#                         print("1", src_vocab.itos[1])
#                         print(selected_sent_context)
#                         # test for correctly gathered sub context information
#                         max_sub_context_len = torch.max(sub_context_len)
#                         for ii in range(len(sub_context_len.data)):
#                             print(sub_context.data.transpose(0,1)[ii])
#                             for jj in range(max_sub_context_len.data[0]):
# #                                 print("ii",ii)
# #                                 print("jj",jj)
#                                 print(src_vocab.itos[sub_context.data[jj][ii][0]], end = ' ')
#                             print()
#                             for jj in range(max_sub_context_len.data[0]):
#                                 print(src_vocab.itos[src.data[jj][ii][0]], end = ' ')
#                             print()                
#                             for jj in range(max_sub_context_len.data[0]):
#                                 print(batch.context_mask.data[jj][ii], end = ' ')
#                             print()                
#                             print()                        
#                         input()

                
#                     print("Model line:1201 sentence_memory bank element size", sentence_memory_bank[i].size())
#                     print("Model line:1202 context input size", context_inputs[i].size())
            is_empty_sentence_memory_bank = False
        
        # add padding
        max_origin_length = torch.max(lengths)
#         print("Model line:1207 max origin length", max_origin_length)

        for i in range(len(batch)):
            diff = max_origin_length - sentence_memory_bank[i].size(0)
            if diff == 0:
                continue
            dummy_tensor = Variable(torch.zeros((diff, sentence_memory_bank[i].size(1))),  requires_grad=False).cuda()
#             print("Model line:1211 dummy tensor", dummy_tensor)
            sentence_memory_bank[i] = torch.cat((sentence_memory_bank[i], dummy_tensor), 0)
#             print(i, sentence_memory_bank[i].size())

#         for i in range(len(batch)):
#             print(i, context_inputs[i].size()) # 1 * hidden_size
#         print("Model line:1193 origin length", lengths)
#         print("Model line:1193 sentence_memory_bank", torch.stack(sentence_memory_bank))
#         print("Model line:1193 sentence_memory_bank", torch.stack(context_inputs))
        sentence_memory_bank = torch.stack(sentence_memory_bank).transpose(0,1)
        context_inputs = torch.stack(context_inputs).transpose(0,1)
        
        ####################
        # context encoding
        ####################        
        
#         print("Model line:1225 sentence_memory_bank size", sentence_memory_bank.size()) # batch size
#         print("Model line:1226 context inputs size", context_inputs.size()) # seq_len * batch * hidden
        
        # sort context inputs dim for packing
        sorted_context_lengths, sorted_indices = torch.sort(batch.context_lengthes, descending=True)
#         print("Model line:1234 context sorted_context_lengths",  sorted_context_lengths)
#         print("Model line:1235 context sorted_indices",  sorted_indices) # seq_len * batch * hidden
        
        sorted_context_inputs = torch.index_select(context_inputs, 1, sorted_indices)
#         print("Model line:1226 sorted context inputs size", sorted_context_inputs.size())

        _, reversed_indices = torch.sort(sorted_indices, descending=True)
       
        # memory_bank : seq_len * batch * hidden
        # enc_final : (hn, cn) : dir * layer * batch * hidden

        
        # rearrange dim to original dim
        context_enc_final, context_memory_bank = self.context_encoder(sorted_context_inputs, sorted_indices)
        context_memory_bank = torch.index_select(context_memory_bank, 1, reversed_indices)
        
        # LSTM
        if isinstance(context_enc_final, tuple):
            context_enc_final = (torch.index_select(context_enc_final[0], 1, reversed_indices), torch.index_select(context_enc_final[1], 1, reversed_indices))
        else: 
            context_enc_final = torch.index_select(context_enc_final, 1, reversed_indices)
        
#         print("Model line:1260 context memory bank",  context_memory_bank)
#         print("Model line:1261 context enc_final",  context_enc_final) # seq_len * batch * hidden 

        return sentence_memory_bank, context_memory_bank, context_enc_final
        

    def forward(self, src, tgt, lengths, dec_state=None, batch=None):
        """Forward propagate a `src` and `tgt` pair for training.
            Possible initialized with a beginning decoder state.

            Args:
                src (:obj:`Tensor`):
                    a source sequence passed to encoder.
                    typically for inputs this will be a padded :obj:`LongTensor`
                    of size `[len x batch x features]`. however, may be an
                    image or other generic input depending on encoder.
                tgt (:obj:`LongTensor`):
                     a target sequence of size `[tgt_len x batch]`.
                lengths(:obj:`LongTensor`): the src lengths, pre-padding `[batch]`.
                dec_state (:obj:`DecoderState`, optional): initial decoder state
            Returns:
                (:obj:`FloatTensor`, `dict`, :obj:`onmt.Models.DecoderState`):

                     * decoder output `[tgt_len x batch x hidden]`
                     * dictionary attention dists of `[tgt_len x batch x src_len]`
                     * final decoder state
        """
#         print("model line:602", self.obj_f)
        assert batch is not None

        sentence_memory_bank, context_memory_bank, context_enc_final = self.hierarchical_encode(src, lengths, batch)
#         print("model line:1344 c m bank", context_memory_bank)

        tgt = tgt[:-1]  # exclude last target from inputs        
        
        enc_state = \
            self.decoder.init_decoder_state(src, context_memory_bank, context_enc_final)
            
        # have been gained
        # sentence_memory_bank
            # src_map, context_mask
        # context_enc_final, context_memory_bank
            # context_lengthes
            
        sentence_memory_length = torch.sum((batch.context_mask >= 0).long(), 0)
        context_memory_length = batch.context_lengthes
        
#         print("model line 1351, sentence_memory_length", sentence_memory_length) # batch size
#         print("model line 1351, context_memory_length", context_memory_length) # batch size

        self.decoder.init_attn_history() # init attn history in decoder for new attention
        
#(self, tgt, sentence_memory_bank, context_memory_bank, state, sentence_memory_bank, context_memory_bank, state, sentence_memory_lengths, context_memory_lengths, context_mask, idf_weights=None):        
#         print("model line:1366 c m bank", context_memory_bank)
        decoder_outputs, dec_state, attns, context_attns = \
            self.decoder(tgt, sentence_memory_bank, context_memory_bank, 
                         enc_state if dec_state is None
                         else dec_state,
                         sentence_memory_length,
                         context_memory_length,
                         batch.context_mask)
        if self.multigpu:
            # Not yet supported on multi-gpu
            dec_state = None
            attns = None            
            
        self.context_attns = context_attns
        return decoder_outputs, attns, dec_state

    
    def sample(self, src, tgt, lengths, dec_states=None, batch=None, mode="sample", eos_index=3):
        """Forward propagate a `src` and `tgt` pair for training.
        Possible initialized with a beginning decoder state.

        Args:
            src (:obj:`Tensor`):
                a source sequence passed to encoder.
                typically for inputs this will be a padded :obj:`LongTensor`
                of size `[len x batch x features]`. however, may be an
                image or other generic input depending on encoder.
            tgt (:obj:`LongTensor`):
                 a target sequence of size `[tgt_len x batch]`.
            lengths(:obj:`LongTensor`): the src lengths, pre-padding `[batch]`.
            dec_states (:obj:`DecoderState`, optional): initial decoder state
        Returns:
            (:obj:`FloatTensor`, `dict`, :obj:`onmt.Models.DecoderState`):

                 * decoder output `[tgt_len x batch x hidden]`
                 * dictionary attention dists of `[tgt_len x batch x src_len]`
                 * final decoder state
        """
        
#         print("model line:602", self.obj_f)
        tgt = tgt[:-1]  # exclude last target from inputs

        enc_final, memory_bank = self.encoder(src, lengths)
        dec_states = \
            self.decoder.init_decoder_state(src, memory_bank, enc_final)
       
#         print("model line 662 enc_state", enc_final)
#         print("model line 663 enc_state hidden", enc_state.hidden)
#         input()
            
        self.decoder.init_attn_history() # init attn history in decoder for new attention
        
        assert self.obj_f == "rl" or self.obj_f == "hybrid"
        assert batch != None
        def _unbottle(v, batch_size):
            return v.view(-1, batch_size, v.size(1))
            
        # Initialize local and return variables.
        decoder_outputs = []
        probs = []
        out_indices = []
        attns = {"std": []}
        if self.decoder._copy:
            attns["copy"] = []
        if self.decoder._coverage:
            attns["coverage"] = []
                     
#         print("model line:622, tgt.size", tgt.size(0))
        for i in range(tgt.size(0)):
            # initial bos tokens
            if i == 0:
                inp = tgt[0].unsqueeze(0)
#                 print("model line:679 inp", inp.view(1,-1))
#                 print("model line:689 inpu.req", inp.requires_grad)
            # test code
            else:
#                 print("model line:682 inp", i, out_indices[-1])            
#                 input()
                inp = Variable(out_indices[-1], requires_grad=False).cuda().unsqueeze(0).unsqueeze(2)
                



            # Turn any copied words to UNKs
            # 0 is unk
            if self.decoder._copy or self.decoder.copy_attn:
                inp = inp.masked_fill(
                    inp.gt(len(batch.dataset.fields['tgt'].vocab) - 1), 0)
#             inp = inp.unsqueeze(2)             
#             print("model line:682 inp", i, inp)

            # Run one step.
            dec_out, dec_states, attn = self.decoder(
                inp, memory_bank, dec_states, memory_lengths=lengths)
            dec_out = dec_out.squeeze(0)
            # dec_out: beam x rnn_size                    

            # (b) Compute a vector of batch x beam word scores.
            if not self.decoder._copy and not self.decoder.copy_attn:
                out = self.generator.forward(dec_out).data
                out = unbottle(out)
                # beam x tgt_vocab
                beam_attn = unbottle(attn["std"])
            else:
                out = self.generator.forward(dec_out,
                                                   attn["copy"].squeeze(0),
                                                   batch.src_map)
                # batch x (tgt_vocab + extra_vocab)
#                 print("model line:714 out dec_out", dec_out) # variable
#                 print("model line:714 out prob", out) # variable
#                 print("model line:729 out data prob", out) # batch * vocab size
                out = batch.dataset.collapse_copy_scores(
                    _unbottle(out.data, len(batch)),
                    batch, batch.dataset.fields["tgt"].vocab, batch.dataset.src_vocabs)
                # batch x tgt_vocab
#                 out_data = out_data.log()
                out = out.log().squeeze(0) # batch_size * (tgt_vocab + ext)
                
#             print("model line:729 out mode", mode)
#             print("model line:729 out data prob", out) # batch * vocab size
#             input()

            if mode == "greedy":
#                 _, index = torch.max(out.data,1)
                _, index = torch.max(out,1)
#                 print("model line 734, index", index)
#                 print("model line 735, out", out)
#                 prob = out.gather(1, Variable(index.unsqueeze(1), requires_grad=False)) # batchsize * 1
                # prob : 1*batch size
                # index : 1*batcch size
            elif mode == "sample": 
#                 print("model line:734 multi", torch.exp(out.squeeze(0)))
#                 input()
#                 index = torch.multinomial(torch.exp(out.data.squeeze(0)),1) # batchsize*1
                index = torch.multinomial(torch.exp(out),1) # batchsize*1
#                 print("model line 734, index", index)
#                 print("model line 735, out", out)
#                 prob = out.gather(1, Variable(index, requires_grad=False)) # batchsize * 1
    
            index = index.view(-1)
        
            # control intermediate termination 
            # eos index is 3
            # padding index is 1
            if i == 0:
                unfinished = index != eos_index
            else:
                unfinished = unfinished * ( out_indices[-1] != eos_index )
            if unfinished.sum() == 0:
                break
                
            if len(out_indices) > 0:
#                 print("Model line 766: index", index)
                index = index * unfinished.type_as(index) + (unfinished == 0).type_as(index) # pad 1
#                 print("Model line 766: index", index)
#                 print("Model line 767: unfinished", unfinished)
            
#             probs += [prob.view(-1)]
            out_indices += [index]

#             print("model line:726 out_indices")
#             for index in out_indices:
#                 print(index.view(1,-1))
            
                    
            # update history
#             print("model line:679 dec out", dec_out.size()) # batch * hidden size
            decoder_outputs += [dec_out]
#             print("model line:677 attns", attn["std"][0])
            attns["std"] += [attn["std"][0]]
            # Update the coverage attention.
            if self.decoder._coverage:
                coverage = coverage + p_attn \
                    if coverage is not None else p_attn
                attns["coverage"] += attn["coverage"][0]

            # Run the forward pass of the copy attention layer.
            # TODO
#             if self.decoder._copy and not self.decoder._reuse_copy_attn:
#                 _, copy_attn = self.decoder.copy_attn(dec_out,
#                                               memory_bank.transpose(0, 1))
#                 attns["copy"] += [copy_attn]
            if self.decoder._copy:
                attns["copy"] = attns["std"] 
                
        # Update the state with the result.
        final_output = decoder_outputs[-1]
        coverage = None
        if "coverage" in attns:
            coverage = attns["coverage"][-1].unsqueeze(0)
        dec_states.update_state(dec_out, final_output.unsqueeze(0), coverage)

        # Concatenates sequence of tensors along a new dimension.
        decoder_outputs = torch.stack(decoder_outputs)
        for k in attns:
            attns[k] = torch.stack(attns[k])              

        # pad eos if not finished 3 is eos token
        # remove
#         if unfinished.sum() != 0:
#             unfinished = unfinished * ( out_indices[-1] != eos_index )
#             index = eos_index * unfinished.type_as(index) +  (unfinished == 0).type_as(index) # pad 1
#             out_indices += [index]    
    #         probs = torch.stack(probs)
        out_indices = torch.stack(out_indices)
#         print("model line 770 probx, out_indices", probs.size(), out_indices.size()) # tgt_len * batch size
#         input("model line:771")
    
#         return decoder_outputs, attns, dec_states, probs, out_indices
        return decoder_outputs, attns, dec_states, out_indices
    

class DecoderState(object):
    """Interface for grouping together the current state of a recurrent
    decoder. In the simplest case just represents the hidden state of
    the model.  But can also be used for implementing various forms of
    input_feeding and non-recurrent models.

    Modules need to implement this to utilize beam search decoding.
    """
    def detach(self):
        for h in self._all:
            if h is not None:
                h.detach_()

    def beam_update(self, idx, positions, beam_size):
        for e in self._all:
            sizes = e.size()
            br = sizes[1]
            if len(sizes) == 3:
                sent_states = e.view(sizes[0], beam_size, br // beam_size,
                                     sizes[2])[:, :, idx]
            else:
                sent_states = e.view(sizes[0], beam_size,
                                     br // beam_size,
                                     sizes[2],
                                     sizes[3])[:, :, idx]

            sent_states.data.copy_(
                sent_states.data.index_select(1, positions))


class RNNDecoderState(DecoderState):
    def __init__(self, hidden_size, rnnstate):
        """
        Args:
            hidden_size (int): the size of hidden layer of the decoder.
            rnnstate: final hidden state from the encoder.
                transformed to shape: layers x batch x (directions*dim).
        """
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.coverage = None

        # Init the input feed.
        batch_size = self.hidden[0].size(1)
        h_size = (batch_size, hidden_size)
        self.input_feed = Variable(self.hidden[0].data.new(*h_size).zero_(),
                                   requires_grad=False).unsqueeze(0)

    @property
    def _all(self):
        return self.hidden + (self.input_feed,)

    def update_state(self, rnnstate, input_feed, coverage):
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.input_feed = input_feed
        self.coverage = coverage

    def repeat_beam_size_times(self, beam_size):
        """ Repeat beam_size times along batch dimension. """
        vars = [Variable(e.data.repeat(1, beam_size, 1), volatile=True)
                for e in self._all]
        self.hidden = tuple(vars[:-1])
        self.input_feed = vars[-1]

        