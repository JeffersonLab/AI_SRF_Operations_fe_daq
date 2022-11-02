"""This module contains any network specific code.  Currently defines single class for accessing system trust store."""

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.ssl_ import create_urllib3_context


class SSLContextAdapter(HTTPAdapter):
    """An HTTPAdapter that loads the default system SSL trust store

    This is needed since the requests module ships with its own CA cert store that does not include the JLab PKI"""

    def init_poolmanager(self, *args, **kwargs):
        """Overrides the parent method to include call to load_default_certs()"""
        context = create_urllib3_context()
        kwargs['ssl_context'] = context
        context.load_default_certs()  # this loads the OS default trusted CA certs
        return super(SSLContextAdapter, self).init_poolmanager(*args, **kwargs)
