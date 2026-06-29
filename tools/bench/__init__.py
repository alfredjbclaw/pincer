"""pincer SWE-bench harness plumbing.

Turns a Pincer run into official-harness predictions and grades them with the
real SWE-bench evaluator — never Pincer's own sandbox (the research is emphatic:
do not self-grade). Named `bench`, not `swebench`, so it never shadows the
official `swebench` pip package when grading.
"""
