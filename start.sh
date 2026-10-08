#!/usr/bin/env sh
# Démarrage Railway : migrations, compte RH initial, puis serveur.
set -e
export FLASK_APP=wsgi.py
flask db upgrade
flask init-admin
exec gunicorn wsgi:app --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --threads 4 --timeout 180 --access-logfile -
