from som.interpreter.ast.frame import (
    read_frame,
    write_frame,
    write_inner,
    read_inner,
    FRAME_AND_INNER_RCVR_IDX,
    get_inner_as_context,
)
from som.interpreter.bc.bytecodes import bytecode_length, Bytecodes
from som.interpreter.bc.frame import (
    stack_top,
    get_stack_element,
    stack_pop,
    stack_push,
    get_block_at,
    stack_set_top,
    get_self_dynamically,
)
from som.interpreter.control_flow import ReturnException
from som.interpreter.send import lookup_and_send_2, lookup_and_send_3
from som.vmobjects.array import Array
from som.vmobjects.block_bc import BcBlock

from rlib import jit


def _do_push_global(bytecode_index, frame, method):
    from som.vm.current import current_universe

    global_name = method.get_constant(bytecode_index)

    # Get the global from the universe
    glob = current_universe.get_global(global_name)

    if glob:
        # Push the global onto the stack
        stack_push(frame, glob)
    else:
        result = lookup_and_send_2(
            get_self_dynamically(frame), global_name, "unknownGlobal:"
        )
        stack_push(frame, result)


def _do_pop_field(bytecode_index, frame, method):
    # Handle the pop field bytecode
    field_index = method.get_bytecode(bytecode_index + 1)
    ctx_level = method.get_bytecode(bytecode_index + 2)

    # Set the field with the computed index to the value popped from the stack
    get_self(frame, ctx_level).set_field(field_index, stack_pop(frame))


def _do_super_send(bytecode_index, frame, method):
    signature = method.get_constant(bytecode_index)

    receiver_class = method.get_holder().get_super_class()
    invokable = receiver_class.lookup_invokable(signature)

    num_args = invokable.get_number_of_signature_arguments()
    receiver = get_stack_element(frame, num_args - 1)

    if invokable:
        method.set_inline_cache(bytecode_index, receiver_class, invokable)
        if num_args == 1:
            bc = Bytecodes.q_super_send_1
        elif num_args == 2:
            bc = Bytecodes.q_super_send_2
        elif num_args == 3:
            bc = Bytecodes.q_super_send_3
        else:
            bc = Bytecodes.q_super_send_n
        method.set_bytecode(bytecode_index, bc)
        invoke_invokable_slow_path(invokable, num_args, receiver, frame)
    else:
        _send_does_not_understand(receiver, frame, invokable.get_signature())


@jit.unroll_safe
def _do_return_non_local(frame, ctx_level):
    # get result from stack
    result = stack_top(frame)

    # Compute the context for the non-local return
    block = get_block_at(frame, ctx_level)

    # Make sure the block context is still on the stack
    if not block.is_outer_on_stack():
        # Try to recover by sending 'escapedBlock:' to the sending object
        # this can get a bit nasty when using nested blocks. In this case
        # the "sender" will be the surrounding block and not the object
        # that actually sent the 'value' message.
        block = read_frame(frame, FRAME_AND_INNER_RCVR_IDX)
        sender = get_self_dynamically(frame)

        # ... and execute the escapedBlock message instead
        return lookup_and_send_2(sender, block, "escapedBlock:")

    raise ReturnException(result, block.get_on_stack_marker())


def invoke_invokable_slow_path(invokable, num_args, receiver, frame):
    if num_args == 1:
        result = invokable.invoke_1(receiver)
        stack_set_top(frame, result)
    elif num_args == 2:
        result = invokable.invoke_2(receiver, stack_pop(frame))
        stack_set_top(frame, result)
    elif num_args == 3:
        arg2 = stack_pop(frame)
        arg1 = stack_pop(frame)
        result = invokable.invoke_3(receiver, arg1, arg2)
        stack_set_top(frame, result)
    else:
        invokable.invoke_n(frame)


@jit.unroll_safe
def interpret(method, frame):
    from som.vm.current import current_universe

    current_bc_idx = 0
    while True:
        # since methods cannot contain loops (all loops are done via primitives)
        # profiling only needs to be done on pc = 0
        if current_bc_idx == 0:
            jitdriver.can_enter_jit(
                bytecode_index=current_bc_idx, method=method, frame=frame
            )
        jitdriver.jit_merge_point(
            bytecode_index=current_bc_idx, method=method, frame=frame
        )

        bytecode = method.get_bytecode(current_bc_idx)

        # Get the length of the current bytecode
        bc_length = bytecode_length(bytecode)

        # Compute the next bytecode index
        next_bc_idx = current_bc_idx + bc_length

        # Handle the current bytecode
        if bytecode == Bytecodes.halt:
            return stack_top(frame)
        if bytecode == Bytecodes.dup:
            stack_push(frame, stack_top(frame))
        elif bytecode == Bytecodes.push_frame:
            assert method.get_bytecode(current_bc_idx + 2) == 0
            stack_push(
                frame, read_frame(frame, method.get_bytecode(current_bc_idx + 1))
            )
        elif bytecode == Bytecodes.push_inner:
            idx = method.get_bytecode(current_bc_idx + 1)
            ctx_level = method.get_bytecode(current_bc_idx + 2)

            if ctx_level == 0:
                value = read_inner(frame, idx)
            else:
                block = get_block_at(frame, ctx_level)
                value = block.get_from_outer(idx)

            stack_push(frame, value)
        elif bytecode == Bytecodes.push_field:
            field_index = method.get_bytecode(current_bc_idx + 1)
            ctx_level = method.get_bytecode(current_bc_idx + 2)
            stack_push(frame, get_self(frame, ctx_level).get_field(field_index))
        elif bytecode == Bytecodes.push_block:
            block_method = method.get_constant(current_bc_idx)
            stack_push(frame, BcBlock(block_method, get_inner_as_context(frame)))
        elif bytecode == Bytecodes.push_constant:
            stack_push(frame, method.get_constant(current_bc_idx))
        elif bytecode == Bytecodes.push_global:
            _do_push_global(current_bc_idx, frame, method)
        elif bytecode == Bytecodes.pop:
            stack_pop(frame)
        elif bytecode == Bytecodes.pop_frame:
            assert method.get_bytecode(current_bc_idx + 2) == 0
            write_frame(
                frame, method.get_bytecode(current_bc_idx + 1), stack_pop(frame)
            )
        elif bytecode == Bytecodes.pop_inner:
            idx = method.get_bytecode(current_bc_idx + 1)
            ctx_level = method.get_bytecode(current_bc_idx + 2)
            value = stack_pop(frame)
            if ctx_level == 0:
                write_inner(frame, idx, value)
            else:
                block = get_block_at(frame, ctx_level)
                block.set_outer(idx, value)
        elif bytecode == Bytecodes.pop_field:
            _do_pop_field(current_bc_idx, frame, method)
        elif bytecode == Bytecodes.send_1:
            signature = method.get_constant(current_bc_idx)
            receiver = get_stack_element(frame, 0)

            invokable = _lookup(
                receiver.get_class(current_universe), signature, method, current_bc_idx
            )
            if invokable:
                result = invokable.invoke_1(receiver)
                stack_set_top(frame, result)
            else:
                _send_does_not_understand(receiver, frame, signature)
        elif bytecode == Bytecodes.send_2:
            signature = method.get_constant(current_bc_idx)
            receiver = get_stack_element(frame, 1)

            invokable = _lookup(
                receiver.get_class(current_universe), signature, method, current_bc_idx
            )
            if invokable:
                result = invokable.invoke_2(receiver, stack_pop(frame))
                stack_set_top(frame, result)
            else:
                _send_does_not_understand(receiver, frame, signature)
        elif bytecode == Bytecodes.send_3:
            signature = method.get_constant(current_bc_idx)
            receiver = get_stack_element(frame, 2)

            invokable = _lookup(
                receiver.get_class(current_universe), signature, method, current_bc_idx
            )
            if invokable:
                arg2 = stack_pop(frame)
                arg1 = stack_pop(frame)
                result = invokable.invoke_3(receiver, arg1, arg2)
                stack_set_top(frame, result)
            else:
                _send_does_not_understand(receiver, frame, signature)
        elif bytecode == Bytecodes.send_n:
            signature = method.get_constant(current_bc_idx)
            receiver = get_stack_element(
                frame, signature.get_number_of_signature_arguments() - 1
            )

            invokable = _lookup(
                receiver.get_class(current_universe), signature, method, current_bc_idx
            )
            if invokable:
                result = invokable.invoke_n(frame)
            else:
                _send_does_not_understand(receiver, frame, signature)
        elif bytecode == Bytecodes.super_send:
            _do_super_send(current_bc_idx, frame, method)
        elif bytecode == Bytecodes.return_local:
            return stack_top(frame)
        elif bytecode == Bytecodes.return_non_local:
            return _do_return_non_local(frame, method.get_bytecode(current_bc_idx + 1))
        elif bytecode == Bytecodes.return_self:
            return read_frame(frame, FRAME_AND_INNER_RCVR_IDX)
        elif bytecode == Bytecodes.inc:
            val = stack_top(frame)
            from som.vmobjects.integer import Integer
            from som.vmobjects.double import Double
            from som.vmobjects.biginteger import BigInteger

            if isinstance(val, Integer):
                result = val.prim_inc()
            elif isinstance(val, Double):
                result = val.prim_inc()
            elif isinstance(val, BigInteger):
                result = val.prim_inc()
            else:
                return _not_yet_implemented()
            stack_set_top(frame, result)
        elif bytecode == Bytecodes.dec:
            val = stack_top(frame)
            from som.vmobjects.integer import Integer
            from som.vmobjects.double import Double
            from som.vmobjects.biginteger import BigInteger

            if isinstance(val, Integer):
                result = val.prim_dec()
            elif isinstance(val, Double):
                result = val.prim_dec()
            elif isinstance(val, BigInteger):
                result = val.prim_dec()
            else:
                return _not_yet_implemented()
            stack_set_top(frame, result)
        elif bytecode == Bytecodes.q_super_send_1:
            invokable = method.get_inline_cache_invokable(current_bc_idx)
            receiver = get_stack_element(frame, 0)
            result = invokable.invoke_1(receiver)
            stack_set_top(frame, result)
        elif bytecode == Bytecodes.q_super_send_2:
            invokable = method.get_inline_cache_invokable(current_bc_idx)
            receiver = get_stack_element(frame, 1)
            result = invokable.invoke_2(receiver, stack_pop(frame))
            stack_set_top(frame, result)
        elif bytecode == Bytecodes.q_super_send_3:
            invokable = method.get_inline_cache_invokable(current_bc_idx)
            receiver = get_stack_element(frame, 2)
            arg2 = stack_pop(frame)
            arg1 = stack_pop(frame)
            result = invokable.invoke_3(receiver, arg1, arg2)
            stack_set_top(frame, result)
        elif bytecode == Bytecodes.q_super_send_n:
            invokable = method.get_inline_cache_invokable(current_bc_idx)
            invokable.invoke_n(frame)
        elif bytecode == Bytecodes.push_local:
            method.patch_variable_access(current_bc_idx)
            # retry bytecode after patching
            next_bc_idx = current_bc_idx
        elif bytecode == Bytecodes.push_argument:
            method.patch_variable_access(current_bc_idx)
            # retry bytecode after patching
            next_bc_idx = current_bc_idx
        elif bytecode == Bytecodes.pop_local:
            method.patch_variable_access(current_bc_idx)
            # retry bytecode after patching
            next_bc_idx = current_bc_idx
        elif bytecode == Bytecodes.pop_argument:
            method.patch_variable_access(current_bc_idx)
            # retry bytecode after patching
            next_bc_idx = current_bc_idx
        else:
            _unknown_bytecode(bytecode, current_bc_idx, method)

        current_bc_idx = next_bc_idx


def _not_yet_implemented():
    raise Exception("Not yet implemented")


def _unknown_bytecode(bytecode, bytecode_idx, method):
    from som.compiler.bc.disassembler import dump_method

    dump_method(method, "")
    raise Exception(
        "Unknown bytecode: " + str(bytecode) + " at bci: " + str(bytecode_idx)
    )


def get_self(frame, ctx_level):
    # Get the self object from the interpreter
    if ctx_level == 0:
        return read_frame(frame, FRAME_AND_INNER_RCVR_IDX)
    return get_block_at(frame, ctx_level).get_from_outer(FRAME_AND_INNER_RCVR_IDX)


def _lookup(receiver_class, selector, method, bytecode_index):
    # First try the inline cache
    cached_class = method.get_inline_cache_class(bytecode_index)
    if cached_class == receiver_class:
        invokable = method.get_inline_cache_invokable(bytecode_index)
    else:
        if not cached_class:
            invokable = receiver_class.lookup_invokable(selector)
            method.set_inline_cache(bytecode_index, receiver_class, invokable)
        else:
            # the bytecode index after the send is used by the selector constant,
            # and can be used safely as another cache item
            cached_class = method.get_inline_cache_class(bytecode_index + 1)
            if cached_class == receiver_class:
                invokable = method.get_inline_cache_invokable(bytecode_index + 1)
            else:
                invokable = receiver_class.lookup_invokable(selector)
                if not cached_class:
                    method.set_inline_cache(
                        bytecode_index + 1, receiver_class, invokable
                    )
    return invokable


def _send_does_not_understand(receiver, frame, selector):
    # ignore self
    number_of_arguments = selector.get_number_of_signature_arguments() - 1
    arguments_array = Array.from_size(number_of_arguments)

    # Remove all arguments and put them in the freshly allocated array
    i = number_of_arguments - 1
    while i >= 0:
        arguments_array.set_indexable_field(i, stack_pop(frame))
        i -= 1

    stack_pop(frame)  # pop self from stack
    result = lookup_and_send_3(
        receiver, selector, arguments_array, "doesNotUnderstand:arguments:"
    )
    stack_push(frame, result)


def get_printable_location(bytecode_index, method):
    from som.vmobjects.method_bc import BcAbstractMethod
    from som.interpreter.bc.bytecodes import bytecode_as_str

    assert isinstance(method, BcAbstractMethod)
    bc = method.get_bytecode(bytecode_index)
    return "%s @ %d in %s" % (
        bytecode_as_str(bc),
        bytecode_index,
        method.merge_point_string(),
    )


jitdriver = jit.JitDriver(
    name="Interpreter",
    greens=["bytecode_index", "method"],
    reds=["frame"],
    # virtualizables=['frame'],
    get_printable_location=get_printable_location,
    # the next line is a workaround around a likely bug in RPython
    # for some reason, the inlining heuristics default to "never inline" when
    # two different jit drivers are involved (in our case, the primitive
    # driver, and this one).
    # the next line says that calls involving this jitdriver should always be
    # inlined once (which means that things like Integer>>< will be inlined
    # into a while loop again, when enabling this drivers).
    should_unroll_one_iteration=lambda bytecode_index, method: True,
)


def jitpolicy(_driver):
    from rpython.jit.codewriter.policy import JitPolicy  # pylint: disable=import-error

    return JitPolicy()
