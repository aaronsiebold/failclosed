.PHONY: test mutants example all

test:
	python3 -m unittest discover -s tests

mutants:
	python3 -m failclosed.mutate

example:
	python3 examples/outbound.py

all: test example mutants
