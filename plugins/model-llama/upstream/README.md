# Vendored upstream reference

`run.c` and `LICENSE` are copied unchanged from [karpathy/llama2.c](https://github.com/karpathy/llama2.c)
at commit `350e04fe35433e6d2941dce5a1f53308f87058eb` (MIT license).

`run.c` is the upstream native implementation that defines L0 for llama2.c models
(requirements §3.4.3, §11.1). The plugin's NumPy port is checked against it: the test
compiles `run.c` and compares greedy generations text for text.
