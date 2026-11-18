Arithmetic expression evaluator in `services/api/calc.py`, standard library only, without `eval` or `exec`.

- `evaluate(expr: str)` supports numbers (ints and decimals), `+ - * /`, parentheses, unary minus and spaces
- Usual precedence: `*` and `/` before `+` and `-`, left to right
- Division by zero raises `ZeroDivisionError`; anything malformed (empty, dangling operator, letters,
  two numbers in a row, unbalanced parentheses) raises `ValueError`
