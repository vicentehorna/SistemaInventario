#!/usr/bin/env bash
# Solo si despliegas con runtime Python nativo en Render (sin Docker).
# Recomendado: usar Docker + Dockerfile (este script no se ejecuta en ese caso).
set -o errexit

pip install --upgrade pip
pip install -r requirements-prod.txt
