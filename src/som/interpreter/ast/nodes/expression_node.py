from rtruffle.node import Node


class ExpressionNode(Node):
    def __init__(self, source_section):
        Node.__init__(self, source_section)

    def create_trivial_method(self, _signature):  # pylint: disable=no-self-use
        return None

    def is_trivial_in_sequence(self):  # pylint: disable=no-self-use
        return False
