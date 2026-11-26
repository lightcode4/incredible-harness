import os

# The packaged app's Python has no system CA bundle, so raw urllib (GitHub sign-in, update
# checks) fails TLS verification. Point OpenSSL at certifi's bundle before anything connects.
try:
    import certifi
    _ca = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
except Exception:
    pass

from myagent.desktop import main

main()
