from som.compiler.method_generation_context import MethodGenerationContextBase
from som.interpreter.ast.frame import FRAME_AND_INNER_RCVR_IDX
from som.interpreter.bc.bytecodes import (
    bytecode_stack_effect,
    bytecode_stack_effect_depends_on_send,
    Bytecodes,
    is_compiler_only_bytecode,
    bytecode_length_in_compiler,
)
from som.vmobjects.primitive import empty_primitive
from som.vmobjects.method_bc import BcMethod, BcMethodNoNonLocalReturns


class MethodGenerationContext(MethodGenerationContextBase):
    def __init__(self, universe, outer=None):
        MethodGenerationContextBase.__init__(self, universe, outer)

        self._literals = []
        self._finished = False
        self._bytecode = []

        # keep a list of arguments and locals for easy access
        # when patching bytecodes on method completion
        self._arg_list = []
        self._local_list = []

    def add_argument(self, arg):
        argument = MethodGenerationContextBase.add_argument(self, arg)
        self._arg_list.append(argument)
        return argument

    def add_local(self, local):
        local = MethodGenerationContextBase.add_local(self, local)
        self._local_list.append(local)
        return local

    def assemble(self, _dummy):
        if self._primitive:
            return empty_primitive(self._signature.get_embedded_string(), self.universe)

        frame_details = self.prepare_frame()
        max_stack_size = self._compute_stack_depth_and_patch_frame_access()
        before_stack_start = frame_details.size_frame - 1
        size_frame = frame_details.size_frame + max_stack_size + 1  # +1 for the StackPtr

        num_locals = len(self._locals)

        if self.needs_to_catch_non_local_returns:
            meth = BcMethod(
                list(self._literals),
                num_locals,
                max_stack_size,
                len(self._bytecode),
                self._signature,
                frame_details.arg_inner_access,
                size_frame,
                frame_details.size_inner,
                before_stack_start,
            )
        else:
            meth = BcMethodNoNonLocalReturns(
                list(self._literals),
                num_locals,
                max_stack_size,
                len(self._bytecode),
                self._signature,
                frame_details.arg_inner_access,
                size_frame,
                frame_details.size_inner,
                before_stack_start,
            )

        # copy bytecodes into method
        i = 0
        for bytecode in self._bytecode:
            meth.set_bytecode(i, bytecode)
            i += 1

        # return the method - the holder field is to be set later on!
        return meth

    def get_argument(self, index, context):
        if context > 0:
            return self.outer_genc.get_argument(index, context - 1)
        return self._arg_list[index]

    def get_local(self, index, context):
        if context > 0:
            return self.outer_genc.get_local(index, context - 1)
        return self._local_list[index]

    def _compute_stack_depth_and_patch_frame_access(self):
        depth = 0
        max_depth = 0
        i = 0

        while i < len(self._bytecode):
            bc = self._bytecode[i]

            if bytecode_stack_effect_depends_on_send(bc):
                signature = self._literals[self._bytecode[i + 1]]
                depth += bytecode_stack_effect(
                    bc, signature.get_number_of_signature_arguments()
                )
            else:
                depth += bytecode_stack_effect(bc)
                if is_compiler_only_bytecode(bc):
                    if bc == Bytecodes.push_argument:
                        var = self.get_argument(
                            self._bytecode[i + 1], self._bytecode[i + 2]
                        )
                        self._bytecode[i] = var.get_push_bytecode()
                    elif bc == Bytecodes.pop_argument:
                        var = self.get_argument(
                            self._bytecode[i + 1], self._bytecode[i + 2]
                        )
                        self._bytecode[i] = var.get_pop_bytecode()
                    elif bc == Bytecodes.push_local:
                        var = self.get_local(
                            self._bytecode[i + 1], self._bytecode[i + 2]
                        )
                        self._bytecode[i] = var.get_push_bytecode()
                    elif bc == Bytecodes.pop_local:
                        var = self.get_local(
                            self._bytecode[i + 1], self._bytecode[i + 2]
                        )
                        self._bytecode[i] = var.get_pop_bytecode()
                    else:
                        raise Exception("Unsupported bytecode?")
                    assert FRAME_AND_INNER_RCVR_IDX <= var.access_idx <= 255, (
                        "Expected variable access index to be in valid range, but was "
                        + str(var.access_idx)
                    )
                    self._bytecode[i + 1] = var.access_idx

            i += bytecode_length_in_compiler(bc)

            if depth > max_depth:
                max_depth = depth

        return max_depth

    def is_finished(self):
        return self._finished

    def set_finished(self):
        self._finished = True

    def remove_last_bytecode(self):
        self._bytecode = self._bytecode[:-1]

    def add_literal_if_absent(self, lit):
        if lit in self._literals:
            return self._literals.index(lit)

        self._literals.append(lit)
        return len(self._literals) - 1

    def add_literal(self, lit):
        i = len(self._literals)

        assert i < 128
        self._literals.append(lit)

        return i

    def update_literal(self, old_val, index, new_val):
        assert self._literals[index] == old_val
        self._literals[index] = new_val

    def find_var(self, var, ctx_level):
        if var in self._locals:
            return FindVarResult(self._locals[var], ctx_level, False)

        if var in self._arguments:
            return FindVarResult(self._arguments[var], ctx_level, True)

        if self.outer_genc:
            return self.outer_genc.find_var(var, ctx_level + 1)
        return None

    def get_max_context_level(self):
        if self.outer_genc is None:
            return 0
        return 1 + self.outer_genc.get_max_context_level()

    def add_bytecode(self, bytecode):
        self._bytecode.append(bytecode)

    def has_bytecode(self):
        return len(self._bytecode) > 0

    def find_literal_index(self, lit):
        return self._literals.index(lit)


class FindVarResult(object):
    def __init__(self, var, context, is_argument):
        self.var = var
        self.context = context
        self.is_argument = is_argument

    def mark_accessed(self):
        self.var.mark_accessed(self.context)


def create_bootstrap_method(universe):
    """Create a fake bootstrap method to simplify later frame traversal"""
    bootstrap_method = BcMethod(
        [], 0, 2, 1, universe.symbol_for("bootstrap"), [], 4, 0, 0
    )

    bootstrap_method.set_bytecode(0, Bytecodes.halt)
    bootstrap_method.set_holder(universe.system_class)
    return bootstrap_method
