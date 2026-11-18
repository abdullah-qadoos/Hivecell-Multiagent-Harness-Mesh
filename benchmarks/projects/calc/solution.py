import re

TOKEN = re.compile(r"\s*(?:(\d+\.?\d*|\.\d+)|(.))")


def evaluate(expr):
    tokens = []
    for num, op in TOKEN.findall(expr):
        if num:
            tokens.append(float(num) if "." in num else int(num))
        elif op.strip():
            if op not in "+-*/()":
                raise ValueError(f"unexpected {op!r}")
            tokens.append(op)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def expression():
        value = term()
        while peek() in ("+", "-"):
            value = value + term() if take() == "+" else value - term()
        return value

    def term():
        value = factor()
        while peek() in ("*", "/"):
            if take() == "*":
                value *= factor()
            else:
                value /= factor()
        return value

    def factor():
        t = peek()
        if t == "-":
            take()
            return -factor()
        if t == "(":
            take()
            value = expression()
            if peek() != ")":
                raise ValueError("unbalanced parentheses")
            take()
            return value
        if isinstance(t, (int, float)):
            return take()
        raise ValueError("expected a number")

    result = expression()
    if pos != len(tokens):
        raise ValueError("unexpected input after expression")
    return result
