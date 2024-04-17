from __future__ import annotations

import ast as pyast
import inspect
import re
import sys
import textwrap
from collections import ChainMap

import astor
from asdl_adt.validators import ValidationError

from .API_types import ProcedureBase
from .builtins import *
from .configs import Config
from .LoopIR import UAST, PAST, front_ops
from .prelude import *

from typing import Any, Callable, Union, NoReturn, Optional
import copy
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Helpers


class ParseError(Exception):
    pass


class SizeStub:
    def __init__(self, nm):
        assert isinstance(nm, Sym)
        self.nm = nm


def str_to_mem(name):
    return getattr(sys.modules[__name__], name)


@dataclass
class SourceInfo:
    src_file: str
    src_line_offset: int
    src_col_offset: int

    def get_src_info(self, node: pyast.AST):
        return SrcInfo(
            filename=self.src_file,
            lineno=node.lineno + self.src_line_offset,
            col_offset=node.col_offset + self.src_col_offset,
            end_lineno=(
                None
                if node.end_lineno is None
                else node.end_lineno + self.src_line_offset
            ),
            end_col_offset=(
                None
                if node.end_col_offset is None
                else node.end_col_offset + self.src_col_offset
            ),
        )


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Top-level decorator


def get_ast_from_python(f: Callable[..., Any]) -> tuple[pyast.stmt, SourceInfo]:
    # note that we must dedent in case the function is defined
    # inside of a local scope
    rawsrc = inspect.getsource(f)
    src = textwrap.dedent(rawsrc)
    n_dedent = len(re.match("^(.*)", rawsrc).group()) - len(
        re.match("^(.*)", src).group()
    )

    # convert into AST nodes; which should be a module with a single node
    module = pyast.parse(src)
    assert len(module.body) == 1

    return module.body[0], SourceInfo(
        src_file=inspect.getsourcefile(f),
        src_line_offset=inspect.getsourcelines(f)[1] - 1,
        src_col_offset=n_dedent,
    )


@dataclass
class BoundLocal:
    val: Any


Local = Optional[BoundLocal]


@dataclass
class FrameScope:
    frame: inspect.frame

    def get_globals(self) -> dict[str, Any]:
        return self.frame.f_globals

    def read_locals(self) -> dict[str, Local]:
        return {
            var: (
                BoundLocal(self.frame.f_locals[var])
                if var in self.frame.f_locals
                else None
            )
            for var in self.frame.f_code.co_varnames
            + self.frame.f_code.co_cellvars
            + self.frame.f_code.co_freevars
        }


@dataclass
class DummyScope:
    global_dict: dict[str, Any]
    local_dict: dict[str, Any]

    def get_globals(self) -> dict[str, Any]:
        return self.global_dict

    def read_locals(self) -> dict[str, Any]:
        return self.local_dict.copy()


Scope = Union[DummyScope, FrameScope]


def get_parent_scope(*, depth) -> Scope:
    """
    Get global and local environments for context capture purposes
    """
    stack_frames = inspect.stack()
    assert len(stack_frames) >= depth
    frame = stack_frames[depth].frame
    return FrameScope(frame)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Pattern-Parser top-level, invoked on strings rather than as a decorator


def pattern(s, filename=None, lineno=None):
    assert isinstance(s, str)

    src = s
    n_dedent = 0
    if filename is not None:
        srcfilename = filename
        srclineno = lineno
    else:
        srcfilename = "string"
        srclineno = 0

    module = pyast.parse(src)
    assert isinstance(module, pyast.Module)

    parser = Parser(
        module.body,
        SourceInfo(
            src_file=srcfilename, src_line_offset=srclineno, src_col_offset=n_dedent
        ),
        parent_scope=DummyScope({}, {}),
        is_fragment=True,
    )
    return parser.result()


class QuoteReplacer(pyast.NodeTransformer):
    def __init__(
        self,
        parser_parent: "Parser",
        unquote_env: "UnquoteEnv",
        stmt_collector: Optional[list[pyast.stmt]] = None,
    ):
        self.stmt_collector = stmt_collector
        self.unquote_env = unquote_env
        self.parser_parent = parser_parent

    def visit_With(self, node: pyast.With) -> pyast.Any:
        if (
            len(node.items) == 1
            and isinstance(node.items[0].context_expr, pyast.Name)
            and node.items[0].context_expr.id == "quote"
            and isinstance(node.items[0].context_expr.ctx, pyast.Load)
        ):
            assert (
                self.stmt_collector != None
            ), "Reached quote block with no buffer to place quoted statements"

            def quote_callback(_f):
                self.stmt_collector.extend(
                    Parser(
                        node.body,
                        self.parser_parent.src_info,
                        parent_scope=get_parent_scope(depth=2),
                        is_quote_stmt=True,
                        parent_exo_locals=self.parser_parent.exo_locals,
                    ).result()
                )

            callback_name = self.unquote_env.register_quote_callback(quote_callback)
            return pyast.FunctionDef(
                name=self.unquote_env.mangle_name(QUOTE_BLOCK_PLACEHOLDER_PREFIX),
                args=pyast.arguments(
                    posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
                ),
                body=node.body,
                decorator_list=[pyast.Name(id=callback_name, ctx=pyast.Load())],
            )
        else:
            return super().generic_visit(node)

    def visit_Call(self, node: pyast.Call) -> Any:
        if (
            isinstance(node.func, pyast.Name)
            and node.func.id == "quote"
            and len(node.keywords) == 0
            and len(node.args) == 1
        ):

            def quote_callback(_e):
                return Parser(
                    node.args[0],
                    self.parser_parent.src_info,
                    parent_scope=get_parent_scope(depth=2),
                    is_quote_expr=True,
                    parent_exo_locals=self.parser_parent.exo_locals,
                ).result()

            callback_name = self.unquote_env.register_quote_callback(quote_callback)
            return pyast.Call(
                func=pyast.Name(id=callback_name, ctx=pyast.Load()),
                args=[
                    pyast.Lambda(
                        args=pyast.arguments(
                            posonlyargs=[],
                            args=[],
                            kwonlyargs=[],
                            kw_defaults=[],
                            defaults=[],
                        ),
                        body=node,
                    )
                ],
                keywords=[],
            )
        else:
            return super().generic_visit(node)


QUOTE_CALLBACK_PREFIX = "__quote_callback"
QUOTE_BLOCK_PLACEHOLDER_PREFIX = "__quote_block"
OUTER_SCOPE_HELPER = "__outer_scope"
NESTED_SCOPE_HELPER = "__nested_scope"
UNQUOTE_RETURN_HELPER = "__unquote_val"


@dataclass
class UnquoteEnv:
    parent_globals: dict[str, Any]
    parent_locals: dict[str, Local]

    def mangle_name(self, prefix: str) -> str:
        index = 0
        while True:
            mangled_name = f"{prefix}{index}"
            if (
                mangled_name not in self.parent_locals
                and mangled_name not in self.parent_globals
            ):
                return mangled_name
            index += 1

    def register_quote_callback(self, quote_callback: Callable[[], None]) -> str:
        mangled_name = self.mangle_name(QUOTE_CALLBACK_PREFIX)
        self.parent_locals[mangled_name] = BoundLocal(quote_callback)
        return mangled_name

    def interpret_quote_block(self, stmts: list[pyast.stmt]) -> Any:
        bound_locals = {
            name: val.val for name, val in self.parent_locals.items() if val is not None
        }
        unbound_names = {
            name for name, val in self.parent_locals.items() if val is None
        }
        exec(
            compile(
                pyast.fix_missing_locations(
                    pyast.Module(
                        body=[
                            pyast.FunctionDef(
                                name=OUTER_SCOPE_HELPER,
                                args=pyast.arguments(
                                    posonlyargs=[],
                                    args=[
                                        pyast.arg(arg=arg) for arg in self.parent_locals
                                    ],
                                    kwonlyargs=[],
                                    kw_defaults=[],
                                    defaults=[],
                                ),
                                body=[
                                    *(
                                        [
                                            pyast.Delete(
                                                targets=[
                                                    pyast.Name(
                                                        id=name,
                                                        ctx=pyast.Del(),
                                                    )
                                                    for name in unbound_names
                                                ]
                                            )
                                        ]
                                        if len(unbound_names) != 0
                                        else []
                                    ),
                                    pyast.FunctionDef(
                                        name=NESTED_SCOPE_HELPER,
                                        args=pyast.arguments(
                                            posonlyargs=[],
                                            args=[],
                                            kwonlyargs=[],
                                            kw_defaults=[],
                                            defaults=[],
                                        ),
                                        body=stmts,
                                        decorator_list=[],
                                    ),
                                    pyast.Return(
                                        value=pyast.Call(
                                            func=pyast.Name(
                                                id=NESTED_SCOPE_HELPER,
                                                ctx=pyast.Load(),
                                            ),
                                            args=[],
                                            keywords=[],
                                        )
                                    ),
                                ],
                                decorator_list=[],
                            ),
                            pyast.Assign(
                                targets=[
                                    pyast.Name(
                                        id=UNQUOTE_RETURN_HELPER, ctx=pyast.Store()
                                    )
                                ],
                                value=pyast.Call(
                                    func=pyast.Name(
                                        id=OUTER_SCOPE_HELPER,
                                        ctx=pyast.Load(),
                                    ),
                                    args=[
                                        (
                                            pyast.Constant(value=None)
                                            if val is None
                                            else pyast.Name(id=name, ctx=pyast.Load())
                                        )
                                        for name, val in self.parent_locals.items()
                                    ],
                                    keywords=[],
                                ),
                            ),
                        ],
                        type_ignores=[],
                    )
                ),
                "",
                "exec",
            ),
            self.parent_globals,
            bound_locals,
        )
        return bound_locals[UNQUOTE_RETURN_HELPER]

    def interpret_quote_expr(self, expr: pyast.expr):
        return self.interpret_quote_block([pyast.Return(value=expr)])


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Parser Pass object


class Parser:
    def __init__(
        self,
        module_ast,
        src_info,
        parent_scope=None,
        is_fragment=False,
        as_func=False,
        as_config=False,
        instr=None,
        is_quote_stmt=False,
        is_quote_expr=False,
        parent_exo_locals=None,
    ):
        self.module_ast = module_ast
        self.parent_scope = parent_scope
        self.exo_locals = ChainMap() if parent_exo_locals is None else parent_exo_locals
        self.src_info = src_info
        self.is_fragment = is_fragment

        self.push()

        builtins = {"sin": sin, "relu": relu, "select": select}
        if is_fragment:
            self.AST = PAST
        else:
            self.AST = UAST
        # add builtins
        for key, val in builtins.items():
            self.exo_locals[key] = val

        if as_func:
            self._cached_result = self.parse_fdef(module_ast, instr=instr)
        elif as_config:
            self._cached_result = self.parse_cls(module_ast)
        elif is_fragment:
            is_expr = False
            if len(module_ast) == 1:
                s = module_ast[0]
                special_cases = list(builtins.keys()) + ["stride"]
                if isinstance(s, pyast.Expr) and (
                    not isinstance(s.value, pyast.Call)
                    or s.value.func.id in special_cases
                ):
                    is_expr = True

            if is_expr:
                self._cached_result = self.parse_expr(s.value)
            else:
                self._cached_result = self.parse_stmt_block(module_ast)
        elif is_quote_expr:
            self._cached_result = self.parse_expr(module_ast)
        elif is_quote_stmt:
            self._cached_result = self.parse_stmt_block(module_ast)
        else:
            assert False, "parser mode configuration unsupported"
        self.pop()

    def getsrcinfo(self, ast):
        return self.src_info.get_src_info(ast)

    def result(self):
        return self._cached_result

    def push(self):
        self.exo_locals = self.exo_locals.new_child()

    def pop(self):
        self.exo_locals = self.exo_locals.parents

    # - # - # - # - # - # - # - # - # - # - # - # - # - # - # - #
    # parser helper routines

    def err(self, node, errstr, origin=None):
        raise ParseError(f"{self.getsrcinfo(node)}: {errstr}") from origin

    def eval_expr(self, expr):
        assert isinstance(expr, pyast.expr)
        return UnquoteEnv(
            self.parent_scope.get_globals(),
            {
                **self.parent_scope.read_locals(),
                **{k: BoundLocal(v) for k, v in self.exo_locals.items()},
            },
        ).interpret_quote_expr(expr)

    # - # - # - # - # - # - # - # - # - # - # - # - # - # - # - #
    # structural parsing rules...

    def parse_fdef(self, fdef, instr=None):
        assert isinstance(fdef, pyast.FunctionDef)

        fargs = fdef.args
        bad_arg_syntax_errmsg = """
    Exo expects function arguments to not use these python features:
      - position-only arguments
      - unnamed (position or keyword) arguments (i.e. *varargs, **kwargs)
      - keyword-only arguments
      - default argument values
    """
        if (
            len(fargs.posonlyargs) > 0
            or fargs.vararg is not None
            or len(fargs.kwonlyargs) > 0
            or len(fargs.kw_defaults) > 0
            or fargs.kwarg is not None
            or len(fargs.defaults) > 0
        ):
            self.err(fargs, bad_arg_syntax_errmsg)

        # process each argument in order
        # we will assume for now that all sizes come first
        # sizes = []
        args = []
        names = set()
        for a in fargs.args:
            if a.annotation is None:
                self.err(a, "expected argument to be typed, i.e. 'x : T'")
            tnode = a.annotation

            typ, mem = self.parse_arg_type(tnode)
            if a.arg in names:
                self.err(a, f"repeated argument name: '{a.arg}'")
            names.add(a.arg)
            nm = Sym(a.arg)
            if isinstance(typ, UAST.Size):
                self.exo_locals[a.arg] = SizeStub(nm)
            else:
                # note we don't need to stub the index variables
                self.exo_locals[a.arg] = nm
            args.append(UAST.fnarg(nm, typ, mem, self.getsrcinfo(a)))

        # return types are non-sensical for Exo, b/c it models procedures
        if fdef.returns is not None:
            self.err(fdef, "Exo does not support function return types")

        # parse out any assertions at the front of the statement block
        # first split the assertions out
        first_non_assert = 0
        while first_non_assert < len(fdef.body) and isinstance(
            fdef.body[first_non_assert], pyast.Assert
        ):
            first_non_assert += 1
        assertions = fdef.body[0:first_non_assert]
        pyast_body = fdef.body[first_non_assert:]

        # then parse out predicates from the assertions
        preds = []
        for a in assertions:
            if a.msg is not None:
                self.err(a, "Exo procedure assertions should not have messages")

            # stride-expr handling
            a = a.test
            preds.append(self.parse_expr(a))

        # parse the procedure body
        body = self.parse_stmt_block(pyast_body)
        return UAST.proc(
            name=fdef.name,
            args=args,
            preds=preds,
            body=body,
            instr=instr,
            srcinfo=self.getsrcinfo(fdef),
        )

    def parse_cls(self, cls):
        assert isinstance(cls, pyast.ClassDef)

        name = cls.name
        if len(cls.bases) > 0:
            self.err(cls, f"expected no base classes in a config definition")
        # ignore cls.keywords and cls.decorator_list

        fields = [self.parse_config_field(stmt) for stmt in cls.body]

        return (name, fields)

    def parse_config_field(self, stmt):
        basic_err = "expected config field definition of the form: " "name : type"
        if isinstance(stmt, pyast.AnnAssign):
            if stmt.value:
                self.err(stmt, basic_err)
            elif not isinstance(stmt.target, pyast.Name):
                self.err(stmt, basic_err)
            name = stmt.target.id

            typ_list = {
                "bool": UAST.Bool(),
                "size": UAST.Size(),
                "index": UAST.Index(),
                "stride": UAST.Stride(),
            }
            for k in Parser._prim_types:
                typ_list[k] = Parser._prim_types[k]
            del typ_list["R"]

            if (
                isinstance(stmt.annotation, pyast.Name)
                and stmt.annotation.id in typ_list
            ):
                typ = typ_list[stmt.annotation.id]
            else:
                self.err(
                    stmt.annotation,
                    "expected one of the following "
                    "types: " + ", ".join(list(typ_list.keys())),
                )

            return name, typ
        else:
            self.err(stmt, basic_err)

    def parse_arg_type(self, node):
        # Arg was of the form ` name : annotation `
        # The annotation will be of one of the following forms
        #   ` type `
        #   ` type @ memory `
        def is_at(x):
            return isinstance(x, pyast.BinOp) and isinstance(x.op, pyast.MatMult)

        # decompose top-level of annotation syntax
        if is_at(node):
            typ_node = node.left
            mem_node = node.right
        else:
            typ_node = node
            mem_node = None

        # detect which sort of type we have here
        is_size = lambda x: isinstance(x, pyast.Name) and x.id == "size"
        is_index = lambda x: isinstance(x, pyast.Name) and x.id == "index"
        is_bool = lambda x: isinstance(x, pyast.Name) and x.id == "bool"
        is_stride = lambda x: isinstance(x, pyast.Name) and x.id == "stride"

        # parse each kind of type here
        if is_size(typ_node):
            if mem_node is not None:
                self.err(
                    node, "size types should not be annotated with " "memory locations"
                )
            return UAST.Size(), None

        elif is_index(typ_node):
            if mem_node is not None:
                self.err(
                    node, "size types should not be annotated with " "memory locations"
                )
            return UAST.Index(), None

        elif is_bool(typ_node):
            if mem_node is not None:
                self.err(
                    node, "size types should not be annotated with " "memory locations"
                )
            return UAST.Bool(), None

        elif is_stride(typ_node):
            if mem_node is not None:
                self.err(
                    node,
                    "stride types should not be annotated with " "memory locations",
                )
            return UAST.Stride(), None

        else:
            typ = self.parse_num_type(typ_node, is_arg=True)

            mem = self.eval_expr(mem_node) if mem_node else None

            return typ, mem

    def parse_alloc_typmem(self, node):
        if isinstance(node, pyast.BinOp) and isinstance(node.op, pyast.MatMult):
            # node.right == Name
            # x[n] @ DRAM
            # x[n] @ lib.scratch
            mem = self.eval_expr(node.right)
            node = node.left
        else:
            mem = None
        typ = self.parse_num_type(node)
        return typ, mem

    _prim_types = {
        "R": UAST.Num(),
        "f16": UAST.F16(),
        "f32": UAST.F32(),
        "f64": UAST.F64(),
        "i8": UAST.INT8(),
        "ui8": UAST.UINT8(),
        "ui16": UAST.UINT16(),
        "i32": UAST.INT32(),
    }

    def parse_num_type(self, node, is_arg=False):
        if isinstance(node, pyast.Subscript):
            if isinstance(node.value, pyast.List):
                if is_arg is not True:
                    self.err(
                        node,
                        "Window expression such as [R] "
                        "should only be used in the function "
                        "signature",
                    )
                if len(node.value.elts) != 1:
                    self.err(
                        node,
                        "Window expression should annotate " "only one type, e.g. [R]",
                    )

                base = node.value.elts[0]
                typ = self.parse_num_type(base)
                is_window = True
            else:
                typ = self.parse_num_type(node.value)
                is_window = False

            if sys.version_info[:3] >= (3, 9):
                # unpack single or multi-arg indexing to list of slices/indices
                if isinstance(node.slice, pyast.Slice):
                    self.err(node, "index-slicing not allowed")
                else:
                    if isinstance(node.slice, pyast.Tuple):
                        dims = node.slice.elts
                    else:
                        dims = [node.slice]
            else:
                if isinstance(node.slice, (pyast.Slice, pyast.ExtSlice)):
                    self.err(node, "index-slicing not allowed")
                else:
                    assert isinstance(node.slice, pyast.Index)
                    if isinstance(node.slice.value, pyast.Tuple):
                        dims = node.slice.value.elts
                    else:
                        dims = [node.slice.value]

            # convert the dimension list into a full tensor type
            exprs = [self.parse_expr(idx) for idx in dims]
            typ = UAST.Tensor(exprs, is_window, typ)

            return typ

        elif isinstance(node, pyast.Name) and node.id in Parser._prim_types:
            return Parser._prim_types[node.id]
        elif (
            isinstance(node, pyast.Call)
            and isinstance(node.func, pyast.Name)
            and node.func.id == "unquote"
        ):
            if len(node.keywords) != 0:
                self.err(node, "Unquote must take non-keyword argument")
            elif len(node.args) != 1:
                self.err(node, "Unquote must take 1 argument")
            else:
                unquote_env = UnquoteEnv(
                    self.parent_scope.get_globals(), self.parent_scope.read_locals()
                )
                quote_replacer = QuoteReplacer(self, unquote_env)
                unquoted = unquote_env.interpret_quote_expr(
                    quote_replacer.visit(copy.deepcopy(node.args[0]))
                )
                if isinstance(unquoted, str) and unquoted in Parser._prim_types:
                    return Parser._prim_types[unquoted]
                else:
                    self.err(node, "Unquote computation did not yield valid type")
        else:
            self.err(node, "unrecognized type: " + astor.dump_tree(node))

    def parse_stmt_block(self, stmts):
        assert isinstance(stmts, list)

        rstmts = []

        for s in stmts:
            if isinstance(s, pyast.With):
                if (
                    len(s.items) == 1
                    and isinstance(s.items[0].context_expr, pyast.Name)
                    and s.items[0].context_expr.id == "unquote"
                    and isinstance(s.items[0].context_expr.ctx, pyast.Load)
                ):
                    unquote_env = UnquoteEnv(
                        self.parent_scope.get_globals(), self.parent_scope.read_locals()
                    )
                    quoted_stmts = []
                    quote_stmt_replacer = QuoteReplacer(self, unquote_env, quoted_stmts)
                    unquote_env.interpret_quote_block(
                        [
                            quote_stmt_replacer.visit(copy.deepcopy(python_s))
                            for python_s in s.body
                        ],
                    )
                    rstmts.extend(quoted_stmts)
                else:
                    self.err(s.id, "Expected unquote")
            # ----- Assginment, Reduction, Var Declaration/Allocation parsing
            elif isinstance(s, (pyast.Assign, pyast.AnnAssign, pyast.AugAssign)):
                # parse the rhs first, if it's present
                rhs = None
                if isinstance(s, pyast.AnnAssign):
                    if s.value is not None:
                        self.err(
                            s, "Variable declaration should not " "have value assigned"
                        )
                    if self.is_fragment:
                        name_node, idxs, _ = self.parse_lvalue(s.target)
                        if len(idxs) > 0:
                            self.err(s.target, "expected simple name in declaration")
                        nm = name_node.id
                        if nm != "_" and not is_valid_name(nm):
                            self.err(name_node, "expected valid name or _")
                        _, sizes, _ = self.parse_array_indexing(s.annotation)
                        rstmts.append(PAST.Alloc(nm, sizes, self.getsrcinfo(s)))
                        continue  # escape rest of case
                else:
                    rhs = self.parse_expr(s.value)

                # parse the lvalue expression
                if isinstance(s, pyast.Assign):
                    if len(s.targets) > 1:
                        self.err(
                            s,
                            "expected only one expression "
                            "on the left of an assignment",
                        )
                    node = s.targets[0]
                    # handle WriteConfigs
                    if isinstance(node, pyast.Attribute):
                        if not isinstance(node.value, pyast.Name):
                            self.err(
                                node,
                                "expected configuration writes "
                                "of the form 'config.field = ...'",
                            )
                        assert isinstance(node.attr, str)

                        if self.is_fragment:
                            assert isinstance(node.value.id, str)
                            rstmts.append(
                                PAST.WriteConfig(
                                    node.value.id, node.attr, self.getsrcinfo(s)
                                )
                            )
                        else:
                            # lookup config and early-exit
                            config_obj = self.eval_expr(node.value)
                            if not isinstance(config_obj, Config):
                                self.err(
                                    node.value,
                                    "expected indexed object " "to be a Config",
                                )

                            # early-exit in this case
                            field_name = node.attr
                            rstmts.append(
                                UAST.WriteConfig(
                                    config_obj, field_name, rhs, self.getsrcinfo(s)
                                )
                            )
                        continue
                    # handle all other lvalue cases
                    else:
                        lvalue_tmp = self.parse_lvalue(node)
                        name_node, idxs, is_window = lvalue_tmp
                    lhs = s.targets[0]
                else:
                    name_node, idxs, is_window = self.parse_lvalue(s.target)
                    lhs = s.target

                if self.is_fragment:
                    # check that the name is valid
                    nm = name_node.id
                    if nm != "_" and not is_valid_name(nm):
                        self.err(name_node, "expected valid name or _")

                    # generate the assignemnt or reduction statement
                    if isinstance(s, pyast.Assign):
                        rstmts.append(PAST.Assign(nm, idxs, rhs, self.getsrcinfo(s)))
                    elif isinstance(s, pyast.AugAssign):
                        if not isinstance(s.op, pyast.Add):
                            self.err(s, "only += reductions currently supported")
                        rstmts.append(PAST.Reduce(nm, idxs, rhs, self.getsrcinfo(s)))
                else:
                    if is_window:
                        self.err(
                            lhs,
                            "cannot perform windowing on "
                            "left-hand-side of an assignment",
                        )
                    if isinstance(s, pyast.AnnAssign) and len(idxs) > 0:
                        self.err(lhs, "expected simple name in declaration")

                    # insert any needed Allocs
                    if isinstance(s, pyast.AnnAssign):
                        nm = Sym(name_node.id)
                        self.exo_locals[name_node.id] = nm
                        typ, mem = self.parse_alloc_typmem(s.annotation)
                        rstmts.append(UAST.Alloc(nm, typ, mem, self.getsrcinfo(s)))

                    # handle cases of ambiguous assignment to undefined
                    # variables
                    if (
                        isinstance(s, pyast.Assign)
                        and len(idxs) == 0
                        and name_node.id not in self.exo_locals
                    ):
                        nm = Sym(name_node.id)
                        self.exo_locals[name_node.id] = nm
                        do_fresh_assignment = True
                    else:
                        do_fresh_assignment = False

                    # get the symbol corresponding to the name on the
                    # left-hand-side
                    if isinstance(s, (pyast.Assign, pyast.AugAssign)):
                        if name_node.id not in self.exo_locals:
                            self.err(name_node, f"variable '{name_node.id}' undefined")
                        nm = self.exo_locals[name_node.id]
                        if isinstance(nm, SizeStub):
                            self.err(
                                name_node,
                                f"cannot write to size variable '{name_node.id}'",
                            )
                        elif not isinstance(nm, Sym):
                            self.err(
                                name_node,
                                f"expected '{name_node.id}' to "
                                f"refer to a local variable",
                            )

                    # generate the assignemnt or reduction statement
                    if do_fresh_assignment:
                        rstmts.append(UAST.FreshAssign(nm, rhs, self.getsrcinfo(s)))
                    elif isinstance(s, pyast.Assign):
                        rstmts.append(UAST.Assign(nm, idxs, rhs, self.getsrcinfo(s)))
                    elif isinstance(s, pyast.AugAssign):
                        if not isinstance(s.op, pyast.Add):
                            self.err(s, "only += reductions currently supported")
                        rstmts.append(UAST.Reduce(nm, idxs, rhs, self.getsrcinfo(s)))

            # ----- For Loop parsing
            elif isinstance(s, pyast.For):
                if len(s.orelse) > 0:
                    self.err(s, "else clause on for-loops unsupported")

                self.push()

                if not isinstance(s.target, pyast.Name):
                    self.err(s.target, "expected simple name for iterator variable")

                if self.is_fragment:
                    itr = s.target.id
                else:
                    itr = Sym(s.target.id)
                    self.exo_locals[s.target.id] = itr

                cond = self.parse_loop_cond(s.iter)
                body = self.parse_stmt_block(s.body)

                if self.is_fragment:
                    lo, hi = cond
                    rstmts.append(PAST.For(itr, lo, hi, body, self.getsrcinfo(s)))
                else:
                    rstmts.append(UAST.For(itr, cond, body, self.getsrcinfo(s)))

                self.pop()

            # ----- If statement parsing
            elif isinstance(s, pyast.If):
                cond = self.parse_expr(s.test)

                self.push()
                body = self.parse_stmt_block(s.body)
                self.pop()
                self.push()
                orelse = self.parse_stmt_block(s.orelse)
                self.pop()

                rstmts.append(self.AST.If(cond, body, orelse, self.getsrcinfo(s)))

            # ----- Sub-routine call parsing
            elif (
                isinstance(s, pyast.Expr)
                and isinstance(s.value, pyast.Call)
                and isinstance(s.value.func, pyast.Name)
            ):
                if self.is_fragment:
                    # handle stride expression
                    if s.value.func.id == "stride":
                        if (
                            len(s.value.keywords) > 0
                            or len(s.value.args) != 2
                            or not isinstance(s.value.args[0], pyast.Name)
                            or not isinstance(s.value.args[1], pyast.Constant)
                            or not isinstance(s.value.args[1].value, int)
                        ):
                            self.err(
                                s.value,
                                "expected stride(...) to "
                                "have exactly 2 arguments: the identifier "
                                "for the buffer we are talking about "
                                "and an integer specifying which dimension",
                            )

                        name = s.value.args[0].id
                        dim = int(s.value.args[1].value)

                        rstmts.append(
                            PAST.StrideExpr(name, dim, self.getsrcinfo(s.value))
                        )
                    else:
                        if len(s.value.keywords) > 0:
                            self.err(
                                s.value,
                                "cannot call procedure() " "with keyword arguments",
                            )

                        args = [self.parse_expr(a) for a in s.value.args]

                        rstmts.append(
                            PAST.Call(s.value.func.id, args, self.getsrcinfo(s.value))
                        )
                else:
                    f = self.eval_expr(s.value.func)
                    if not isinstance(f, ProcedureBase):
                        self.err(
                            s.value.func, f"expected called object " "to be a procedure"
                        )

                    if len(s.value.keywords) > 0:
                        self.err(
                            s.value, "cannot call procedure() " "with keyword arguments"
                        )

                    args = [self.parse_expr(a) for a in s.value.args]

                    rstmts.append(
                        UAST.Call(f.INTERNAL_proc(), args, self.getsrcinfo(s.value))
                    )

            # ----- Pass no-op parsing
            elif isinstance(s, pyast.Pass):
                rstmts.append(self.AST.Pass(self.getsrcinfo(s)))

            # ----- Stmt Hole parsing
            elif (
                isinstance(s, pyast.Expr)
                and isinstance(s.value, pyast.Name)
                and s.value.id == "_"
            ):
                rstmts.append(PAST.S_Hole(self.getsrcinfo(s.value)))

            elif isinstance(s, pyast.Assert):
                self.err(
                    s,
                    "predicate assert should happen at the beginning " "of a function",
                )
            else:
                self.err(s, "unsupported type of statement")

        return rstmts

    def parse_loop_cond(self, cond):
        if isinstance(cond, pyast.Call):
            if isinstance(cond.func, pyast.Name) and cond.func.id in ("par", "seq"):
                if len(cond.keywords) > 0:
                    self.err(
                        cond, "par() and seq() does not support" " named arguments"
                    )
                elif len(cond.args) != 2:
                    self.err(cond, "par() and seq() expects exactly" " 2 arguments")
                lo = self.parse_expr(cond.args[0])
                hi = self.parse_expr(cond.args[1])

                if self.is_fragment:
                    return lo, hi
                else:
                    if cond.func.id == "par":
                        return UAST.ParRange(lo, hi, self.getsrcinfo(cond))
                    else:
                        return UAST.SeqRange(lo, hi, self.getsrcinfo(cond))
            else:
                self.err(
                    cond,
                    "expected for loop condition to be in the form "
                    "'par(...,...)' or 'seq(...,...)'",
                )
        else:
            e_hole = PAST.E_Hole(self.getsrcinfo(cond))
            if self.is_fragment:
                return e_hole, e_hole
            return e_hole

    # parse the left-hand-side of an assignment
    def parse_lvalue(self, node):
        if not isinstance(node, (pyast.Name, pyast.Subscript)):
            self.err(node, "expected lhs of form 'x' or 'x[...]'")
        else:
            return self.parse_array_indexing(node)

    def parse_array_indexing(self, node):
        if isinstance(node, pyast.Name):
            return node, [], False
        elif isinstance(node, pyast.Subscript):
            if sys.version_info[:3] >= (3, 9):
                # unpack single or multi-arg indexing to list of slices/indices
                if isinstance(node.slice, pyast.Tuple):
                    dims = node.slice.elts
                else:
                    dims = [node.slice]
            else:
                if isinstance(node.slice, pyast.Slice):
                    dims = [node.slice]
                elif isinstance(node.slice, pyast.ExtSlice):
                    dims = node.slice.dims
                else:
                    assert isinstance(node.slice, pyast.Index)
                    if isinstance(node.slice.value, pyast.Tuple):
                        dims = node.slice.value.elts
                    else:
                        dims = [node.slice.value]

            if not isinstance(node.value, pyast.Name):
                self.err(node, "expected access to have form 'x' or 'x[...]'")

            is_window = any(isinstance(e, pyast.Slice) for e in dims)
            idxs = [
                (self.parse_slice(e, node) if is_window else self.parse_expr(e))
                for e in dims
            ]

            return node.value, idxs, is_window
        else:
            assert False, "bad case"

    def parse_slice(self, e, node):
        assert not self.is_fragment, "Window PAST unsupported"

        if sys.version_info[:3] >= (3, 9):
            srcinfo = self.getsrcinfo(e)
        else:
            if isinstance(e, pyast.Index):
                e = e.value
                srcinfo = self.getsrcinfo(e)
            else:
                srcinfo = self.getsrcinfo(node)

        if isinstance(e, pyast.Slice):
            lo = None if e.lower is None else self.parse_expr(e.lower)
            hi = None if e.upper is None else self.parse_expr(e.upper)
            if e.step is not None:
                self.err(
                    e,
                    "expected windowing to have the form x[:], "
                    "x[i:], x[:j], or x[i:j], but not x[i:j:k]",
                )

            return UAST.Interval(lo, hi, srcinfo)
        else:
            return UAST.Point(self.parse_expr(e), srcinfo)

    # parse expressions, including values, indices, and booleans
    def parse_expr(self, e):
        if isinstance(e, (pyast.Name, pyast.Subscript)):
            nm_node, idxs, is_window = self.parse_array_indexing(e)

            if self.is_fragment:
                nm = nm_node.id
                if len(idxs) == 0 and nm == "_":
                    return PAST.E_Hole(self.getsrcinfo(e))
                else:
                    return PAST.Read(nm, idxs, self.getsrcinfo(e))
            else:
                parent_globals = self.parent_scope.get_globals()
                parent_locals = self.parent_scope.read_locals()
                if nm_node.id in self.exo_locals:
                    nm = self.exo_locals[nm_node.id]
                elif (
                    nm_node.id in parent_locals
                    and parent_locals[nm_node.id] is not None
                ):
                    nm = parent_locals[nm_node.id].val
                elif nm_node.id in parent_globals:
                    nm = parent_globals[nm_node.id]
                else:
                    nm = None

                if isinstance(nm, SizeStub):
                    nm = nm.nm
                elif isinstance(nm, Sym):
                    pass  # nm is already set correctly
                elif isinstance(nm, (int, float)):
                    if len(idxs) > 0:
                        self.err(
                            nm_node,
                            f"cannot index '{nm_node.id}' because "
                            f"it is the constant {nm}",
                        )
                    else:
                        return UAST.Const(nm, self.getsrcinfo(e))
                else:  # could not resolve name to anything
                    self.err(nm_node, f"variable '{nm_node.id}' undefined")

                if is_window:
                    return UAST.WindowExpr(nm, idxs, self.getsrcinfo(e))
                else:
                    return UAST.Read(nm, idxs, self.getsrcinfo(e))

        elif isinstance(e, pyast.Attribute):
            if not isinstance(e.value, pyast.Name):
                self.err(
                    e, "expected configuration reads " "of the form 'config.field'"
                )

            assert isinstance(e.attr, str)

            config_obj = e.value.id
            if not self.is_fragment:
                config_obj = self.eval_expr(e.value)
                if not isinstance(config_obj, Config):
                    self.err(e.value, "expected indexed object " "to be a Config")

            return self.AST.ReadConfig(config_obj, e.attr, self.getsrcinfo(e))

        elif isinstance(e, pyast.Constant):
            return self.AST.Const(e.value, self.getsrcinfo(e))

        elif isinstance(e, pyast.UnaryOp):
            if isinstance(e.op, pyast.USub):
                arg = self.parse_expr(e.operand)
                return self.AST.USub(arg, self.getsrcinfo(e))
            else:
                opnm = (
                    "+"
                    if isinstance(e.op, pyast.UAdd)
                    else (
                        "not"
                        if isinstance(e.op, pyast.Not)
                        else (
                            "~"
                            if isinstance(e.op, pyast.Invert)
                            else "ERROR-BAD-OP-CASE"
                        )
                    )
                )
                self.err(e, f"unsupported unary operator: {opnm}")

        elif isinstance(e, pyast.BinOp):
            lhs = self.parse_expr(e.left)
            rhs = self.parse_expr(e.right)
            if isinstance(e.op, pyast.Add):
                op = "+"
            elif isinstance(e.op, pyast.Sub):
                op = "-"
            elif isinstance(e.op, pyast.Mult):
                op = "*"
            elif isinstance(e.op, pyast.Div):
                op = "/"
            elif isinstance(e.op, pyast.FloorDiv):
                op = "//"
            elif isinstance(e.op, pyast.Mod):
                op = "%"
            elif isinstance(e.op, pyast.Pow):
                op = "**"
            elif isinstance(e.op, pyast.LShift):
                op = "<<"
            elif isinstance(e.op, pyast.RShift):
                op = ">>"
            elif isinstance(e.op, pyast.BitOr):
                op = "|"
            elif isinstance(e.op, pyast.BitXor):
                op = "^"
            elif isinstance(e.op, pyast.BitAnd):
                op = "&"
            elif isinstance(e.op, pyast.MatMult):
                op = "@"
            else:
                assert False, "unrecognized op"

            if op not in front_ops:
                self.err(e, f"unsupported binary operator: {op}")

            return self.AST.BinOp(op, lhs, rhs, self.getsrcinfo(e))

        elif isinstance(e, pyast.BoolOp):
            assert len(e.values) > 1
            lhs = self.parse_expr(e.values[0])

            if isinstance(e.op, pyast.And):
                op = "and"
            elif isinstance(e.op, pyast.Or):
                op = "or"
            else:
                assert False, "unrecognized op"

            for rhs in e.values[1:]:
                lhs = self.AST.BinOp(op, lhs, self.parse_expr(rhs), self.getsrcinfo(e))

            return lhs

        elif isinstance(e, pyast.Compare):
            assert len(e.ops) == len(e.comparators)

            vals = [self.parse_expr(e.left)] + [
                self.parse_expr(v) for v in e.comparators
            ]
            srcinfo = self.getsrcinfo(e)

            res = None
            for opnode, lhs, rhs in zip(e.ops, vals[:-1], vals[1:]):
                if isinstance(opnode, pyast.Eq):
                    op = "=="
                elif isinstance(opnode, pyast.NotEq):
                    op = "!="
                elif isinstance(opnode, pyast.Lt):
                    op = "<"
                elif isinstance(opnode, pyast.LtE):
                    op = "<="
                elif isinstance(opnode, pyast.Gt):
                    op = ">"
                elif isinstance(opnode, pyast.GtE):
                    op = ">="
                elif isinstance(opnode, pyast.Is):
                    op = "is"
                elif isinstance(opnode, pyast.IsNot):
                    op = "is not"
                elif isinstance(opnode, pyast.In):
                    op = "in"
                elif isinstance(opnode, pyast.NotIn):
                    op = "not in"
                else:
                    assert False, "unrecognized op"
                if op not in front_ops:
                    self.err(e, f"unsupported binary operator: {op}")

                c = self.AST.BinOp(op, lhs, rhs, self.getsrcinfo(e))
                res = c if res is None else self.AST.BinOp("and", res, c, srcinfo)

            return res

        elif isinstance(e, pyast.Call):
            if isinstance(e.func, pyast.Name) and e.func.id == "unquote":
                if len(e.keywords) != 0:
                    self.err(e, "Unquote must take non-keyword argument")
                elif len(e.args) != 1:
                    self.err(e, "Unquote must take 1 argument")
                else:
                    unquote_env = UnquoteEnv(
                        self.parent_scope.get_globals(), self.parent_scope.read_locals()
                    )
                    quote_replacer = QuoteReplacer(self, unquote_env)
                    unquoted = unquote_env.interpret_quote_expr(
                        quote_replacer.visit(copy.deepcopy(e.args[0]))
                    )
                    if isinstance(unquoted, (int, float)):
                        return self.AST.Const(unquoted, self.getsrcinfo(e))
                    elif isinstance(unquoted, self.AST.expr):
                        return unquoted
                    else:
                        self.err(e, "Unquote received input that couldn't be unquoted")
            # handle stride expression
            elif isinstance(e.func, pyast.Name) and e.func.id == "stride":
                if (
                    len(e.keywords) > 0
                    or len(e.args) != 2
                    or not isinstance(e.args[0], pyast.Name)
                    or not (
                        isinstance(e.args[1], pyast.Constant)
                        and isinstance(e.args[1].value, int)
                        or isinstance(e.args[1], pyast.Name)
                        and e.args[1].id == "_"
                    )
                ):
                    self.err(
                        e,
                        "expected stride(...) to "
                        "have exactly 2 arguments: the identifier "
                        "for the buffer we are talking about "
                        "and an integer specifying which dimension",
                    )

                name = e.args[0].id

                if isinstance(e.args[1], pyast.Name):
                    return PAST.StrideExpr(name, None, self.getsrcinfo(e))

                dim = int(e.args[1].value)
                if not self.is_fragment:
                    if name not in self.exo_locals:
                        self.err(e.args[0], f"variable '{name}' undefined")
                    name = self.exo_locals[name]

                return self.AST.StrideExpr(name, dim, self.getsrcinfo(e))

            # handle built-in functions
            else:
                f = self.eval_expr(e.func)
                fname = e.func.id

                if not isinstance(f, BuiltIn):
                    self.err(
                        e.func, f"expected called object " "to be a builtin function"
                    )

                if len(e.keywords) > 0:
                    self.err(
                        f, "cannot call a builtin function " "with keyword arguments"
                    )
                args = [self.parse_expr(a) for a in e.args]

                return self.AST.BuiltIn(f, args, self.getsrcinfo(e))

        else:
            self.err(e, "unsupported form of expression")
