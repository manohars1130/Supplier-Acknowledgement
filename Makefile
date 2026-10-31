PY ?= python3
OUT ?= output
SEED ?= 20261022

.PHONY: help setup generate starter validate evaluate repro frontend test serve all clean
help:
	@echo "make setup      install dependencies (pip)"
	@echo "make all        generate full dataset, validate, evaluate, reproducibility check, rebuild frontend data"
	@echo "make starter    small 500-PO dataset in output_starter/ (same generator)"
	@echo "make test       pytest suite (generates small datasets in temp dirs)"
	@echo "make serve      serve the dashboard at http://localhost:8000"
setup:
	$(PY) -m pip install -r requirements.txt
generate:
	$(PY) generate_data.py --seed $(SEED) --n-pos 10000 --n-events 15000 --out $(OUT)
starter:
	$(PY) generate_data.py --seed $(SEED) --n-pos 500 --n-events 750 --out output_starter
validate:
	$(PY) validate.py --root $(OUT)
evaluate:
	$(PY) evaluate.py --root $(OUT) > /dev/null && echo "metrics -> $(OUT)/docs/metrics_report.json"
repro:
	$(PY) repro_check.py
frontend:
	$(PY) tools/build_frontend_data.py --root $(OUT) --out frontend/data.js
test:
	$(PY) -m pytest -q
serve:
	@echo "Dashboard: http://localhost:8000" && cd frontend && $(PY) -m http.server 8000
all: generate validate evaluate repro frontend
	@echo "ALL STEPS OK"
clean:
	rm -rf output output_starter .pytest_cache __pycache__
