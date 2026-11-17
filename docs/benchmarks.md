# Benchmarks

Reproducible, offline measurements on synthetic data (`scripts/benchmark.py`). They show
orders of magnitude and catch regressions; they are **not** a promise for other machines or
for real provider latency.

```bash
uv run python scripts/benchmark.py --sizes 100,1000,4000
```

## Method

For each size the script generates a synthetic CSV (`demo-data`), creates a workspace and times
each CLI command as a separate process (so each figure includes about 0.1 to 0.2 s of Python
start-up): `import`, `metrics run`, `llm run` with the offline **fake** provider, and
`export`. The peak memory column is the cumulative maximum of the largest child process
(POSIX only), and includes data generation, so it is an upper bound for the analysis stages.

`requests_per_minute` is raised to its maximum (100000) in the benchmark workspace. With the
default of 60, even the fake provider is throttled like a real one: roughly one conversation
per second after the initial burst. Real provider runs are bounded by the provider's latency
and limits, not by this tool (see [runner.md](runner.md)).

## Results

Apple M4, 16 GB, macOS 15, Python 3.11.16, one run each (seed 7):

| Conversations | Messages | import | metrics | llm run (fake) | export | peak MB |
|---:|---:|---:|---:|---:|---:|---:|
| 102 | 2,417 | 0.9s | 0.6s | 1.0s | 0.6s | 49 |
| 1,002 | 25,908 | 2.2s | 0.9s | 6.7s | 0.6s | 153 |
| 4,002 | 107,011 | 5.8s | 1.2s | 16.0s | 0.9s | 385 |

Sizes were requested as 100, 1000 and 4000 conversations (`--sizes`); the generator adds two
extra conversations, so the table reports the **measured** counts: distinct
`conversation_id` values and message records read with `csv.DictReader` (not physical lines,
since messages can span several). Timings vary between runs and with machine load (an
earlier run on the same machine was 2 to 3 times faster); treat them as orders of magnitude.

Time grows roughly linearly with the number of messages in these runs. Conversations here
are short (about 25 messages), so very long chats exercise the context limits in
[llm.md](llm.md) rather than these figures.

## Compatibility

CI runs the test suite on Linux, macOS and Windows with Python 3.11, 3.12 and 3.13, and runs
the quickstart check against the built wheel installed in a clean environment on each OS.
