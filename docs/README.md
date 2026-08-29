# Demo install and screenshots

The images in the README come from an install seeded with invented systems,
corporations and characters. That is not squeamishness: a sovereignty tool's
screenshots are intelligence. Fuel timers say which hub runs dry and when, and
the den board names the people running them. Neither belongs in a public
README, and blurring both leaves an image that is mostly grey.

So the demo data is fictional in the shape the real thing takes. Reagent type
ids are real, because those are public game data and the names have to read
correctly.

## Regenerating

Against a scratch Alliance Auth project -- not your live one, this writes rows:

```bash
DJANGO_SETTINGS_MODULE=myauth.settings.local python docs/demo_seed.py
```

Then serve it and capture. `--insecure` is needed because `DEBUG` is off, and
the plain staticfiles storage because `runserver` serves static through the
finders, which know nothing about the hashed names `collectstatic` writes:

```bash
python manage.py runserver 0.0.0.0:8899 --insecure
```

```bash
HOLDFAST_DEMO_URL=http://127.0.0.1:8899 python docs/screenshots.py <sessionid>
```

The session id is for a logged-in user holding every `holdfast` permission.
`demo_seed.py` creates one called `demo`; give it a password and log in, or
mint a session directly:

```python
from django.contrib.sessions.backends.db import SessionStore
s = SessionStore()
s["_auth_user_id"] = str(user.pk)
s["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
s["_auth_user_hash"] = user.get_session_auth_hash()
s.create()
print(s.session_key)
```
