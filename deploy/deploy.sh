#!/usr/bin/env bash
# تنها راه به‌روزرسانی سرور production.
# هیچ‌وقت روی سرور git clean / git reset --hard نزنید؛ کد فقط این‌جا می‌آید.
set -euo pipefail
APP=/srv/selvi/app; VENV=/srv/selvi/venv; DATA=/srv/selvi/shared
export SELVI_DATA_DIR="$DATA"

# ۱) همیشه اول بکاپ دیتابیس + نگه‌داشتن ۳۰ نسخهٔ آخر
sqlite3 "$DATA/db/db.sqlite3" ".backup '$DATA/backups/pre-deploy-$(date +%F-%H%M).sqlite3'"
ls -1t "$DATA/backups"/pre-deploy-*.sqlite3 2>/dev/null | tail -n +31 | xargs -r rm -f

# ۲) فقط fast-forward — اگر خطا داد یعنی تاریخچه جدا شده؛ دستی جلو نروید و اول بپرسید.
cd "$APP"
git fetch origin
git merge --ff-only origin/main

# ۳) ساخت و سرویس
"$VENV/bin/pip" install -q -r requirements.txt
"$VENV/bin/python" manage.py check
"$VENV/bin/python" manage.py migrate --noinput
"$VENV/bin/python" manage.py collectstatic --noinput

supervisorctl restart selvi
