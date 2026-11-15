PYTHON ?= python

.PHONY: install demo test serve clean

install:
	$(PYTHON) -m pip install -e .

demo:
	$(PYTHON) -m copybook_kit.cli demo

test:
	$(PYTHON) -m pytest -q

serve:
	$(PYTHON) -m copybook_kit.cli serve --port 5000

clean:
	$(PYTHON) -c "import pathlib,shutil; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p, ignore_errors=True) for p in ('build', '.pytest_cache')]"
