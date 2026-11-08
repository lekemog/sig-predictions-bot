# The offline suite exercises trading paths, so it needs a clock before the
# production cutoff in trading_bot.END; SIG_TRADING_END is the test hook.
export SIG_TRADING_END ?= 2099-01-01T00:00:00+00:00

.PHONY: test
test:
	python -m unittest discover -s tests
	python -m unittest discover -s .
