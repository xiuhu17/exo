from __future__ import annotations

import pytest

from .gemmini import *
from .harness_gemmini import GemmTestBuilder


def conv_on_cpu():
    @proc
    def conv_on_cpu_stride_2(
        batch_size : size,
        out_dim    : size,
        out_channel: size,
        kernel_dim : size,
        in_channel : size,
        in_dim     : size,
        padding    : size,
        output     : i8[batch_size, out_dim, out_dim, out_channel],
        bias       : i32[1,out_channel],
        inp        : i8[batch_size, in_dim, in_dim, in_channel],
        weights    : i8[kernel_dim, kernel_dim, in_channel, out_channel],
        act        : bool,
        scale      : f32
        ):

        assert out_dim == (in_dim + 2*padding - kernel_dim)/2 + 1
        assert 0 <= padding < 16
        assert padding < out_dim

        for b in par(0, batch_size):
            for orow in par(0, out_dim):
                for ocol in par(0, out_dim):
                    for och in par(0, out_channel):

                        res : i32
                        res = bias[0,och]
                        for krow in par(0, kernel_dim):
                            for kcol in par(0, kernel_dim):
                                for kch in par(0, in_channel):
                                    if (0 <= orow*2+krow-padding  and orow*2+krow-padding < in_dim and
                                            0 <= ocol*2+kcol-padding and ocol*2+kcol-padding < in_dim):
                                        res += weights[krow,kcol,kch,och] * inp[b,orow*2+krow-padding,ocol*2+kcol-padding,kch]

                        tmp_res1 : f32
                        #tmp_res1 = res
                        #tmp_res1 = tmp_res1 * scale
                        acc_scale(res, tmp_res1, scale)
                        tmp_res2 : i8
                        clamp(tmp_res1, tmp_res2)
                        if act == True:
                            tmp_res2 = relu(tmp_res2)

                        output[b,orow,ocol,och] = tmp_res2

    return conv_on_cpu_stride_2


@proc
def orig_conv_partial_padding(
    batch_size : size,
    out_dim    : size,
    out_channel: size,
    kernel_dim : size,
    in_channel : size,
    in_dim     : size,
    padding    : size,
    output     : i8[batch_size, out_dim, out_dim, out_channel],
    bias       : i32[1, out_channel],
    inp        : i8[batch_size, in_dim, in_dim, in_channel],
    weights    : i8[kernel_dim, kernel_dim, in_channel, out_channel],
    act        : bool,
    scale      : f32,
    b          : index,
    orow       : index,
    one        : f32,
    DIM_SIZE   : size,
    DIM_LO     : index,
    DIM_HI     : index
    ):

    for och in par(0, out_channel/16):

        res : i32[DIM_SIZE,16] @ GEMM_ACCUM
        for l in par(0, DIM_SIZE):
            ld_acc_i32(1, 16, one, bias[ 0:1, 16*och:16*(och+1) ], res[l:l+1, :])

        for kcol in par(0, kernel_dim):
            for krow in par(0, kernel_dim):
                for kch in par(0, in_channel/16):
                    if orow == 0 and krow == 0:
                        pass
                    else:
                        in_scratch : i8[DIM_SIZE,16] @ GEMM_SCRATCH
                        weight_scratch : i8[16,16] @ GEMM_SCRATCH

                        if DIM_LO == 0 and kcol == 0:
                            zero_i8(1, 16, in_scratch[0:1, :])
                            ld_i8_s2(15, 16, one,
                                inp[ b, orow*2+krow-1, 1:DIM_HI*2, 16*kch:16*(kch+1)],
                                in_scratch[1:, :])
                        if DIM_LO*2+kcol-1 >= 0 and DIM_HI*2+kcol-1 > in_dim:
                            ld_i8_s2((in_dim-(DIM_LO*2+kcol-1)+1)/2, 16, one,
                                inp[ b, orow*2+krow-1, DIM_LO*2+kcol-1:, 16*kch:16*(kch+1)],
                                in_scratch[0:(in_dim-(DIM_LO*2+kcol-1)+1)/2, :])
                            zero_i8(DIM_SIZE-((in_dim-(DIM_LO*2+kcol-1)+1)/2), 16, in_scratch[(in_dim-(DIM_LO*2+kcol-1)+1)/2:, :])
                        if DIM_LO*2+kcol-1 >= 0 and DIM_HI*2+kcol-1 <= in_dim:
                            ld_i8_s2(DIM_SIZE, 16, one,
                            inp[ b, orow*2+krow-1, DIM_LO*2+kcol-1:DIM_HI*2+kcol-1, 16*kch:16*(kch+1)],
                            in_scratch)

                        ld_i8(16, 16, one, weights[ krow, kcol, 16*kch:16*(kch+1), 16*och:16*(och+1)], weight_scratch)
                        matmul_acc_i8(DIM_SIZE,16,16,in_scratch,weight_scratch,res)

        st_acc_i8(DIM_SIZE,16, scale, act, res, output[b, orow, DIM_LO:DIM_HI, 16*och:16*(och+1)])

@proc
def conv_partial_padding(
    batch_size : size,
    out_dim    : size,
    out_channel: size,
    kernel_dim : size,
    in_channel : size,
    in_dim     : size,
    padding    : size,
    output     : i8[batch_size, out_dim, out_dim, out_channel],
    bias       : i32[1, out_channel],
    inp        : i8[batch_size, in_dim, in_dim, in_channel],
    weights    : i8[kernel_dim, kernel_dim, in_channel, out_channel],
    act        : bool,
    scale      : f32,
    b          : index,
    orow       : index,
    one        : f32,
    DIM_SIZE   : size,
    DIM_LO     : index,
    DIM_HI     : index
    ):

    config_st_acc_i8(scale, stride(output, 2), act)
    config_ld_i8(one, stride(bias, 0))
    config_ld_i8_s2_id1(one, stride(inp, 2))
    config_ld_i8_id2(one, stride(weights, 2))
    config_matmul()
    for och in par(0, out_channel/16):

        res : i32[DIM_SIZE,16] @ GEMM_ACCUM
        for l in par(0, DIM_SIZE):
            do_ld_acc_i32(1, 16, bias[ 0:1, 16*och:16*(och+1) ], res[l:l+1, :])

        for kcol in par(0, kernel_dim):
            for krow in par(0, kernel_dim):
                for kch in par(0, in_channel/16):
                    if 0 <= orow*2+krow-padding and orow*2+krow-padding < in_dim:
                        in_scratch : i8[DIM_SIZE,16] @ GEMM_SCRATCH
                        weight_scratch : i8[16,16] @ GEMM_SCRATCH

                        if DIM_LO == 0 and kcol == 0:
                            do_zero_i8(1, 16, in_scratch[0:1, :])
                            do_ld_i8_s2_id1(15, 16,
                                inp[ b, orow*2+krow-1, 1:DIM_HI*2, 16*kch:16*(kch+1)],
                                in_scratch[1:, :])
                        if DIM_LO*2+kcol-1 >= 0 and DIM_HI*2+kcol-1 > in_dim:
                            do_ld_i8_s2_id1((in_dim-(DIM_LO*2+kcol-1)+1)/2, 16,
                                inp[ b, orow*2+krow-1, DIM_LO*2+kcol-1:, 16*kch:16*(kch+1)],
                                in_scratch[0:(in_dim-(DIM_LO*2+kcol-1)+1)/2, :])
                            do_zero_i8(DIM_SIZE-((in_dim-(DIM_LO*2+kcol-1)+1)/2), 16, in_scratch[(in_dim-(DIM_LO*2+kcol-1)+1)/2:, :])
                        if DIM_LO*2+kcol-1 >= 0 and DIM_HI*2+kcol-1 <= in_dim:
                            do_ld_i8_s2_id1(DIM_SIZE, 16,
                            inp[ b, orow*2+krow-1, DIM_LO*2+kcol-1:DIM_HI*2+kcol-1, 16*kch:16*(kch+1)],
                            in_scratch)

                        do_ld_i8_id2(16, 16, weights[ krow, kcol, 16*kch:16*(kch+1), 16*och:16*(och+1)], weight_scratch)
                        do_matmul_acc_i8(DIM_SIZE,16,16,in_scratch,weight_scratch,res)

        do_st_acc_i8(DIM_SIZE,16, res, output[b, orow, DIM_LO:DIM_HI, 16*och:16*(och+1)])

def test_conv_13():
    T = GemmTestBuilder('conv_13')
    T.add_body(['gemm_init_mem();',
  #              'init_mem();',
                'gemm_acc_init_mem();',
                'gemmini_flush(0);',
                ''])
    T.add_body(["conv_13_lib_Context *ctxt;"])

    batch_size = 4
    out_channel= 128
    kernel_dim = 3
    in_channel = 128
    padding    = 1
    in_dim     = 56
    out_dim    = int((in_dim + 2*padding - kernel_dim)/2 + 1)
    assert 0 <= padding < 16
    assert padding < out_dim
    assert out_dim == 28

    T.alloc_dram_f32('scale', '0.33')
    T.alloc_dram_2i32('bias', 1, out_channel, 'i')
    T.alloc_dram_4i8('output_cpu', batch_size, out_dim, out_dim, out_channel, 'i')
    T.alloc_dram_4i8('output_gemmini', batch_size, out_dim, out_dim, out_channel, 'j')
    T.alloc_dram_4i8('inp', batch_size, in_dim, in_dim, in_channel, 'k+r')
    T.alloc_dram_4i8('weights', kernel_dim, kernel_dim, in_channel, out_channel, 'k+r')

    @proc
    def conv_13(
        batch_size : size,
        out_dim    : size,
        out_channel: size,
        kernel_dim : size,
        in_channel : size,
        in_dim     : size,
        padding    : size,
        output     : i8[batch_size, out_dim, out_dim, out_channel],
        bias       : i32[1, out_channel],
        inp        : i8[batch_size, in_dim, in_dim, in_channel],
        weights    : i8[kernel_dim, kernel_dim, in_channel, out_channel],
        act        : bool,
        scale      : f32
        ):

        assert out_dim == (in_dim + 2*padding - kernel_dim)/2 + 1
        assert 0 <= padding < 16
        assert padding < out_dim

        one : f32
        one = 1.0
        for b in par(0, batch_size):
            for orow in par(0, out_dim):
                for ocol in par(0, out_dim/16):
                    conv_partial_padding(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding,
                                            output, bias, inp, weights, act, scale, b, orow, one, 16, 16*ocol, 16*(ocol+1))
                if out_dim%16 > 0:
                    conv_partial_padding(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding,
                                            output, bias, inp, weights, act, scale, b, orow, one, out_dim%16, out_dim-out_dim%16, out_dim)


    gemmini = conv_13.partial_eval(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding)
    gemmini = gemmini.inline('conv_partial_padding(_) #0')
    gemmini = gemmini.inline('conv_partial_padding(_) #0')
    gemmini = gemmini.simplify()

    gemmini = gemmini.unroll('ocol')
    gemmini = gemmini.fission_after('config_st_acc_i8(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_s2_id1(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_id2(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_matmul() #0', n_lifts=2)
    gemmini = gemmini.reorder_stmts('for och in _:_ #0', 'config_st_acc_i8(_) #1')
    gemmini = gemmini.reorder_stmts('for och in _:_ #0', 'config_ld_i8(_) #1')
    gemmini = gemmini.reorder_stmts('for och in _:_ #0', 'config_ld_i8_s2_id1(_) #1')
    gemmini = gemmini.reorder_stmts('for och in _:_ #0', 'config_ld_i8_id2(_) #1')
    gemmini = gemmini.fission_after('config_st_acc_i8(_) #1', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8(_) #1', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_s2_id1(_) #1', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_id2(_) #1', n_lifts=2)

    gemmini = gemmini.lift_alloc('res:_', n_lifts=1)
    gemmini = gemmini.lift_alloc('in_scratch:_', n_lifts=5)
    gemmini = gemmini.lift_alloc('weight_scratch:_', n_lifts=4)

    gemmini = gemmini.par_to_seq('for b in _:_')
    gemmini = gemmini.par_to_seq('for orow in _:_')
    gemmini = gemmini.par_to_seq('for och in _:_')

    gemmini = gemmini.lift_alloc('res:_', n_lifts=3)
    gemmini = gemmini.lift_alloc('in_scratch:_', n_lifts=3)
    gemmini = gemmini.lift_alloc('weight_scratch:_', n_lifts=3)

    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #0', 'och #0', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #1', 'och #0', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #2', 'och #0', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #3', 'och #1', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #4', 'och #1', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #5', 'och #1', 0)

    gemmini = gemmini.simplify()

    cpu = conv_on_cpu().partial_eval(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding)
    T.add_proc(cpu)
    T.add_proc(gemmini)

    T.start_timer('cpu')
    T.add_body([f'conv_on_cpu_stride_2(ctxt, output_cpu, bias, inp, weights, false, scale);',
                f'gemmini_fence();'])
    T.stop_timer('cpu', 'Cycles for CPU version')

    T.start_timer('gemmini')
    T.add_body([f'conv_13(ctxt, output_gemmini, bias, inp, weights, false, scale);',
                f'gemmini_fence();'])
    T.stop_timer('gemmini', 'Cycles for GEMMINI version')

    T.add_body([f'if(check_eq_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_cpu, output_gemmini)) {{',
                 '    printf("Correct\\n");',
                 '} else {',
                 '    printf("Results Don\'t Match\\n");',
                 '    printf("Correct Result (output_cpu):\\n");',
                f'    print_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_cpu);',
                 '    printf("Computed Roundtrip (output_gemmini):\\n");',
                f'    print_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_gemmini);',
                 '    exit(1);',
                 '}',
                 ''])

    T.compile().run()

"""
"""

def test_conv_26():
    T = GemmTestBuilder('conv_26')
    T.add_body(['gemm_init_mem();',
  #              'init_mem();',
                'gemm_acc_init_mem();',
                'gemmini_flush(0);',
                ''])
    T.add_body(["conv_26_lib_Context *ctxt;"])

    batch_size = 4
    out_channel= 256
    kernel_dim = 3
    in_channel = 256
    padding    = 1
    in_dim     = 28
    out_dim    = int((in_dim + 2*padding - kernel_dim)/2 + 1)
    assert 0 <= padding < 16
    assert padding < out_dim
    assert out_dim == 14

    T.alloc_dram_f32('scale', '1.0f')
    T.alloc_dram_2i32('bias', 1, out_channel, 'i')
    T.alloc_dram_4i8('output_cpu', batch_size, out_dim, out_dim, out_channel, 'i')
    T.alloc_dram_4i8('output_gemmini', batch_size, out_dim, out_dim, out_channel, 'j')
    T.alloc_dram_4i8('inp', batch_size, in_dim, in_dim, in_channel, 'k+r')
    T.alloc_dram_4i8('weights', kernel_dim, kernel_dim, in_channel, out_channel, 'k+r')

    @proc
    def conv_26(
        batch_size : size,
        out_dim    : size,
        out_channel: size,
        kernel_dim : size,
        in_channel : size,
        in_dim     : size,
        padding    : size,
        output     : i8[batch_size, out_dim, out_dim, out_channel],
        bias       : i32[1, out_channel],
        inp        : i8[batch_size, in_dim, in_dim, in_channel],
        weights    : i8[kernel_dim, kernel_dim, in_channel, out_channel],
        act        : bool,
        scale      : f32
        ):

        assert out_dim == (in_dim + 2*padding - kernel_dim)/2 + 1
        assert 0 <= padding < 16
        assert padding < out_dim

        one : f32
        one = 1.0
        for b in par(0, batch_size):
            for orow in par(0, out_dim):
                for ocol in par(0, out_dim/16):
                    conv_partial_padding(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding,
                                            output, bias, inp, weights, act, scale, b, orow, one, 16, 16*ocol, 16*(ocol+1))

                if out_dim%16 > 0:
                    conv_partial_padding(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding,
                                            output, bias, inp, weights, act, scale, b, orow, one, out_dim%16, out_dim-out_dim%16, out_dim)

    gemmini = conv_26.partial_eval(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding)
    gemmini = gemmini.inline('conv_partial_padding(_) #0')
    gemmini = gemmini.inline('conv_partial_padding(_) #0')
    gemmini = gemmini.simplify()

    gemmini = gemmini.unroll('ocol')
    gemmini = gemmini.fission_after('config_st_acc_i8(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_s2_id1(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_id2(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_matmul() #0', n_lifts=2)

    gemmini = gemmini.lift_alloc('res:_', n_lifts=1)
    gemmini = gemmini.lift_alloc('in_scratch:_', n_lifts=5)
    gemmini = gemmini.lift_alloc('weight_scratch:_', n_lifts=4)

    gemmini = gemmini.par_to_seq('for b in _:_')
    gemmini = gemmini.par_to_seq('for orow in _:_')
    gemmini = gemmini.par_to_seq('for och in _:_')

    gemmini = gemmini.lift_alloc('res:_', n_lifts=3)
    gemmini = gemmini.lift_alloc('in_scratch:_', n_lifts=3)
    gemmini = gemmini.lift_alloc('weight_scratch:_', n_lifts=3)

    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #0', 'och #0', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #1', 'och #0', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #2', 'och #0', 0)

    gemmini = gemmini.simplify()

    cpu = conv_on_cpu().partial_eval(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding)
    T.add_proc(cpu)
    T.add_proc(gemmini)

    T.start_timer('cpu')
    T.add_body([f'conv_on_cpu_stride_2(ctxt, output_cpu, bias, inp, weights, false, scale);',
                f'gemmini_fence();'])
    T.stop_timer('cpu', 'Cycles for CPU version')

    T.start_timer('gemmini')
    T.add_body([f'conv_26(ctxt, output_gemmini, bias, inp, weights, false, scale);',
                f'gemmini_fence();'])
    T.stop_timer('gemmini', 'Cycles for GEMMINI version')

    T.add_body([f'if(check_eq_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_cpu, output_gemmini)) {{',
                 '    printf("Correct\\n");',
                 '} else {',
                 '    printf("Results Don\'t Match\\n");',
                 '    printf("Correct Result (output_cpu):\\n");',
                f'    print_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_cpu);',
                 '    printf("Computed Roundtrip (output_gemmini):\\n");',
                f'    print_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_gemmini);',
                 '    exit(1);',
                 '}',
                 ''])

    T.compile().run()



def test_conv_45():
    T = GemmTestBuilder('conv_45')
    T.add_body(['gemm_init_mem();',
  #              'init_mem();',
                'gemm_acc_init_mem();',
                'gemmini_flush(0);',
                ''])
    T.add_body(["conv_45_lib_Context *ctxt;"])

    batch_size = 4
    out_channel= 512
    kernel_dim = 3
    in_channel = 512
    padding    = 1
    in_dim     = 14
    out_dim    = int((in_dim + 2*padding - kernel_dim)/2 + 1)
    assert 0 <= padding < 16
    assert padding < out_dim
    assert out_dim == 7

    T.alloc_dram_f32('scale', '1.0f')
    T.alloc_dram_2i32('bias', 1, out_channel, 'i')
    T.alloc_dram_4i8('output_cpu', batch_size, out_dim, out_dim, out_channel, 'i')
    T.alloc_dram_4i8('output_gemmini', batch_size, out_dim, out_dim, out_channel, 'j')
    T.alloc_dram_4i8('inp', batch_size, in_dim, in_dim, in_channel, 'k+r')
    T.alloc_dram_4i8('weights', kernel_dim, kernel_dim, in_channel, out_channel, 'k+r')

    @proc
    def conv_45(
        batch_size : size,
        out_dim    : size,
        out_channel: size,
        kernel_dim : size,
        in_channel : size,
        in_dim     : size,
        padding    : size,
        output     : i8[batch_size, out_dim, out_dim, out_channel],
        bias       : i32[1, out_channel],
        inp        : i8[batch_size, in_dim, in_dim, in_channel],
        weights    : i8[kernel_dim, kernel_dim, in_channel, out_channel],
        act        : bool,
        scale      : f32
        ):

        assert out_dim == (in_dim + 2*padding - kernel_dim)/2 + 1
        assert 0 <= padding < 16
        assert padding < out_dim

        one : f32
        one = 1.0
        for b in par(0, batch_size):
            for orow in par(0, out_dim):
                for ocol in par(0, out_dim/16):
                    conv_partial_padding(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding,
                                            output, bias, inp, weights, act, scale, b, orow, one, 16, 16*ocol, 16*(ocol+1))

                if out_dim%16 > 0:
                    conv_partial_padding(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding,
                                            output, bias, inp, weights, act, scale, b, orow, one, out_dim%16, out_dim-out_dim%16, out_dim)

    gemmini = conv_45.partial_eval(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding)
    gemmini = gemmini.inline('conv_partial_padding(_) #0')
    gemmini = gemmini.inline('conv_partial_padding(_) #0')
    gemmini = gemmini.simplify()

    gemmini = gemmini.unroll('ocol')
    gemmini = gemmini.fission_after('config_st_acc_i8(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_s2_id1(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_ld_i8_id2(_) #0', n_lifts=2)
    gemmini = gemmini.fission_after('config_matmul() #0', n_lifts=2)

    gemmini = gemmini.lift_alloc('res:_', n_lifts=1)
    gemmini = gemmini.lift_alloc('in_scratch:_', n_lifts=5)
    gemmini = gemmini.lift_alloc('weight_scratch:_', n_lifts=4)

    gemmini = gemmini.par_to_seq('for b in _:_')
    gemmini = gemmini.par_to_seq('for orow in _:_')
    gemmini = gemmini.par_to_seq('for och in _:_')

    gemmini = gemmini.lift_alloc('res:_', n_lifts=3)
    gemmini = gemmini.lift_alloc('in_scratch:_', n_lifts=3)
    gemmini = gemmini.lift_alloc('weight_scratch:_', n_lifts=3)

    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #0', 'och #0', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #1', 'och #0', 0)
    gemmini = gemmini.add_guard('do_ld_i8_s2_id1(_) #2', 'och #0', 0)

    gemmini = gemmini.simplify()

    cpu = conv_on_cpu().partial_eval(batch_size, out_dim, out_channel, kernel_dim, in_channel, in_dim, padding)
    T.add_proc(cpu)
    T.add_proc(gemmini)

    T.start_timer('cpu')
    T.add_body([f'conv_on_cpu_stride_2(ctxt, output_cpu, bias, inp, weights, false, scale);',
                f'gemmini_fence();'])
    T.stop_timer('cpu', 'Cycles for CPU version')

    T.start_timer('gemmini')
    T.add_body([f'conv_45(ctxt, output_gemmini, bias, inp, weights, false, scale);',
                f'gemmini_fence();'])
    T.stop_timer('gemmini', 'Cycles for GEMMINI version')

    T.add_body([f'if(check_eq_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_cpu, output_gemmini)) {{',
                 '    printf("Correct\\n");',
                 '} else {',
                 '    printf("Results Don\'t Match\\n");',
                 '    printf("Correct Result (output_cpu):\\n");',
                f'    print_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_cpu);',
                 '    printf("Computed Roundtrip (output_gemmini):\\n");',
                f'    print_4i8({batch_size},{out_dim},{out_dim},{out_channel}, output_gemmini);',
                 '    exit(1);',
                 '}',
                 ''])

    T.compile().run()


