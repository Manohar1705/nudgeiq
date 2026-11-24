"""Background worker (step 8d). Celery with Redis as the queue.

Start one with:  celery -A app.worker.celery_app worker --loglevel=INFO
Check it with:   python -m app.worker.check
Schedule:        celery -A app.worker.celery_app beat --loglevel=INFO   (delivers due sends, 8d-2)
"""
