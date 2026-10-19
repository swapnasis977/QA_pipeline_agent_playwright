import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Makes Python's ssl module verify against the OS certificate store instead
# of the bundled certifi list — needed on networks where a corporate root CA
# is trusted by Windows but was never exported into certifi's bundle (the
# cause of "unable to get local issuer certificate" against internal hosts
# and any TLS-inspecting proxy).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
