test:
	python3 -m pytest -q

lint:
	python3 -m pyflakes src tests
