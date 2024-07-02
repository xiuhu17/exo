from __future__ import annotations

import pytest

from exo import proc, DRAM, config, instr
from exo.libs.memories import GEMM_SCRATCH
from exo.stdlib.scheduling import *


# ------- Configuration tests ---------


@pytest.mark.skip()
def test_config_size():
    def new_config_ld():
        @config
        class ConfigLoad:
            size: size

        return ConfigLoad

    ConfigLoad = new_config_ld()

    @proc
    def do_ld_i8(n: size):
        assert n == ConfigLoad.size
        pass

    # @proc
    # def config_ld_i8(
    #    config_n : size
    # ):
    #    ConfigLoad.size = config_n

    @proc
    def foo(N: size):
        # config_ld_i8(N)
        ConfigLoad.size = N
        do_ld_i8(N)

    print(foo)


@pytest.mark.skip()
def test_config_stride():
    def new_config_ld():
        @config
        class ConfigLoad:
            src_stride: stride

        return ConfigLoad

    ConfigLoad = new_config_ld()

    @proc
    def do_ld_i8(n: size, src: [i8][n] @ DRAM):
        assert stride(src, 0) == ConfigLoad.src_stride
        pass

    @proc
    def config_ld_i8(src_stride: stride):
        ConfigLoad.src_stride = src_stride

    @proc
    def foo(N: size, A: i8[N]):
        config_ld_i8(stride(A, 0))
        do_ld_i8()

    print(foo)


def new_config_f32():
    @config
    class ConfigAB:
        a: f32
        b: f32

    return ConfigAB


def new_control_config():
    @config
    class ConfigControl:
        i: index
        s: stride
        b: bool

    return ConfigControl


def test_basic_config(golden):
    ConfigAB = new_config_f32()

    @proc
    def foo(x: f32):
        ConfigAB.a = 32.0
        x = ConfigAB.a

    assert str(foo) == golden


def test_write_loop_const_number(golden):
    ConfigAB = new_config_f32()

    @proc
    def foo(n: size):
        for i in seq(0, n):
            ConfigAB.a = 0.0

    assert str(foo) == golden


def test_write_loop_builtin(golden):
    ConfigAB = new_config_f32()

    @proc
    def foo(n: size):
        for i in seq(0, n):
            ConfigAB.a = sin(1.0)

    assert str(foo) == golden


# Config loop dependency tests
@pytest.mark.skip()
def test_write_loop_varying():
    ConfigAB = new_config_f32()
    with pytest.raises(TypeError, match="The value written to config variable"):

        @proc
        def foo(n: size, A: f32[n]):
            for i in seq(0, n):
                ConfigAB.a = A[i]


@pytest.mark.skip()
def test_write_loop_varying_indirect():
    ConfigAB = new_config_f32()
    with pytest.raises(TypeError, match="The value written to config variable"):

        @proc
        def foo(n: size, A: f32[n]):
            for i in seq(0, n):
                a: f32
                a = A[i]
                ConfigAB.a = a


# NOTE: The following test documents current behavior
#       but it would be very reasonable to make this test
#       non-failing
# Fix is to improve the dependency analysis
@pytest.mark.skip()
def test_write_loop_syntax_check_fail():
    CTRL = new_control_config()

    with pytest.raises(TypeError, match="depends on the loop iteration variable"):

        @proc
        def foo(n: size):
            for i in seq(0, n):
                CTRL.i = i - i


def test_write_all_control(golden):
    CTRL = new_control_config()

    @proc
    def set_all(i: index, s: stride, b: bool):
        CTRL.i = i
        CTRL.s = s
        CTRL.b = b

    assert str(set_all) == golden


# Should the following succeed or fail?
# I think it probably should succeed
def test_loop_complex_guards(golden):
    CTRL = new_control_config()

    @proc
    def foo(n: size):
        for i in seq(0, n):
            if CTRL.i == 3:
                CTRL.i = 4
            if n == n - 1:
                CTRL.i = 3

    assert str(foo) == golden


@pytest.mark.skip()
def test_loop_circular_guards():
    CTRL = new_control_config()

    with pytest.raises(TypeError, match="TODO: Need to determine which error"):

        @proc
        def foo(n: size):
            for i in seq(0, n):
                if CTRL.i == 3:
                    CTRL.i = 4
                elif CTRL.i == 4:
                    CTRL.i = 3


# NOTE: I don't think this should work necessarily
@pytest.mark.skip()  # This should work
def test_config_write7():
    ConfigAB = new_config_f32()

    @proc
    def foo(n: size):
        a: f32
        for i in seq(0, n):
            ConfigAB.a = 3.0
            a = ConfigAB.a


def new_config_ld():
    @config
    class ConfigLoad:
        scale: f32
        src_stride: stride

    return ConfigLoad


def test_stride_with_config(golden):
    ConfigLoad = new_config_ld()

    @proc
    def bar(n: size, src: [i8][n]):
        assert stride(src, 0) == ConfigLoad.src_stride
        pass

    @proc
    def foo(n: size, src: [i8][n]):
        assert stride(src, 0) == ConfigLoad.src_stride
        bar(n, src)

    assert f"{bar}\n{foo}" == golden


def test_config_write(golden):
    @config
    class Config:
        tmp: f32

    @proc
    def foo():
        tmp: f32
        tmp = 0.0

    bar = write_config(foo, foo.find("tmp = _").after(), Config, "tmp", "tmp")
    assert str(bar) == golden


def test_config_bind(golden):
    ConfigLoad = new_config_ld()

    @proc
    def foo(scale: f32):
        for i in seq(0, 10):
            tmp: f32
            tmp = 0.0
            tmp = tmp * scale

    foo = bind_config(foo, "scale", ConfigLoad, "scale")

    assert str(foo) == golden


def test_config_fission(golden):
    ConfigLoad = new_config_ld()

    @proc
    def foo(scale: f32, n: size, m: size, A: f32[n, m]):
        for i in seq(0, n):
            for j in seq(0, m):
                ConfigLoad.scale = scale
                tmp: f32
                tmp = A[i, j]
                tmp = tmp * ConfigLoad.scale

    foo = autofission(foo, foo.find("ConfigLoad.scale = _").after(), n_lifts=2)

    assert str(foo) == golden


def test_ld(golden):
    ConfigLoad = new_config_ld()

    _gemm_config_ld_i8 = (
        "gemmini_extended3_config_ld({src_stride}, " + "{scale}[0], 0, 0);\n"
    )

    @instr(_gemm_config_ld_i8)
    def config_ld_i8(scale: f32, src_stride: stride):
        ConfigLoad.scale = scale
        ConfigLoad.src_stride = src_stride

    _gemm_do_ld_i8 = (
        "gemmini_extended_mvin( {src}.data, " + "((uint64_t) {dst}.data), {m}, {n} );"
    )

    @instr(_gemm_do_ld_i8)
    def do_ld_i8(
        n: size,
        m: size,
        src: i8[n, m] @ DRAM,
        dst: i8[n, 16] @ GEMM_SCRATCH,
    ):
        assert n <= 16
        assert m <= 16
        assert stride(src, 1) == 1
        assert stride(dst, 0) == 16
        assert stride(dst, 1) == 1
        assert stride(src, 0) == ConfigLoad.src_stride

        for i in seq(0, n):
            for j in seq(0, m):
                tmp: f32
                tmp = src[i, j]
                tmp = tmp * ConfigLoad.scale
                dst[i, j] = tmp

    _gemm_ld_i8 = (
        "gemmini_extended3_config_ld({stride(src, 0)}, "
        + "{scale}[0], 0, 0);\n"
        + "gemmini_extended_mvin( {src}.data, "
        + "((uint64_t) {dst}.data), {m}, {n} );"
    )

    @instr(_gemm_ld_i8)
    def ld_i8(
        n: size,
        m: size,
        scale: f32,
        src: i8[n, m] @ DRAM,
        dst: i8[n, 16] @ GEMM_SCRATCH,
    ):
        assert n <= 16
        assert m <= 16
        assert stride(src, 1) == 1
        assert stride(dst, 0) == 16
        assert stride(dst, 1) == 1

        for i in seq(0, n):
            for j in seq(0, m):
                tmp: f32
                tmp = src[i, j]
                tmp = tmp * scale
                dst[i, j] = tmp

    ld_i8 = bind_config(ld_i8, "scale", ConfigLoad, "scale")
    ld_i8 = reorder_stmts(ld_i8, "tmp = src[_] ; ConfigLoad.scale = _")
    ld_i8 = reorder_stmts(ld_i8, "tmp : _ ; ConfigLoad.scale = _")
    ld_i8 = autofission(ld_i8, ld_i8.find("ConfigLoad.scale = _").after(), n_lifts=3)
    ld_i8 = write_config(
        ld_i8,
        ld_i8.find("ConfigLoad.scale = _").after(),
        ConfigLoad,
        "src_stride",
        "stride(src, 0)",
    )
    ld_i8 = replace(ld_i8, "for i in _:_", do_ld_i8)
    ld_i8 = replace(
        ld_i8, "ConfigLoad.scale = _ ; ConfigLoad.src_stride = _", config_ld_i8
    )

    assert f"{config_ld_i8}\n{ld_i8}" == golden
