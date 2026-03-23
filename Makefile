.PHONY: help test test-v test-unit test-integration server client clean

PYTHON     = python3
PYTEST     = python3 -m pytest
SERVER_ADDR ?= ::1
SERVER_PORT ?= 8080
URL        ?= srtp://[::1]:8080/fichier.txt

help:
	@echo "Commandes disponibles :"
	@echo "  make test              Lancer tous les tests"
	@echo "  make test-v            Lancer les tests en mode verbeux"
	@echo "  make test-unit         Uniquement les tests unitaires (protocol)"
	@echo "  make test-integration  Uniquement les tests client/serveur"
	@echo "  make server            Démarrer le serveur  (ADDR=::1 PORT=8080)"
	@echo "  make client            Lancer le client     (URL=...)"
	@echo ""
	@echo "Interopérabilité :"
	@echo "  Place server_ref et client_ref dans src/ puis relance make test"
	@echo ""
	@echo "Exemples :"
	@echo "  make server SERVER_PORT=9000"
	@echo "  make client URL=http://[::1]:9000/mon_fichier.bin"

test:
	$(PYTEST) tests/ -q

test-v:
	$(PYTEST) tests/ -v

test-unit:
	$(PYTEST) tests/test_srtp.py -v

test-integration:
	$(PYTEST) tests/test_integration.py -v

server:
	$(PYTHON) src/server.py $(SERVER_ADDR) $(SERVER_PORT)

client:
	$(PYTHON) src/client.py $(URL)

clean:
	rm -f llm.model
	rm -rf __pycache__ tests/__pycache__ src/__pycache__ .pytest_cache
