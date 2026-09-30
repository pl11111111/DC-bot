"""Parse each link independently; text mentioning a trusted site grants no exemption."""
import re
from urllib.parse import urlsplit

LINK = re.compile(r'(?i)(?:https?://|ftp://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)[^\s<>]+')
TRUSTED_IMAGE_HOSTS = {'cdn.discordapp.com', 'media.discordapp.net'}


def trusted_host(host, allowed):
    try:
        host = host.rstrip('.').encode('idna').decode('ascii').lower()
    except (AttributeError, UnicodeError):
        return False
    return any(host == domain or host.endswith('.' + domain)
               for value in allowed if (domain := value.strip().lower().rstrip('.')))


def has_restricted_link(text, allowed_domains=(), allow_images=False):
    for match in LINK.finditer(text or ''):
        value = match.group().rstrip('.,!?)]}')
        if not re.match(r'(?i)^[a-z]+://', value):
            value = 'https://' + value
        try:
            url = urlsplit(value)
            if url.scheme.lower() not in ('http', 'https') or not url.hostname or url.username is not None:
                return True
            if trusted_host(url.hostname, allowed_domains):
                continue
            # An arbitrary URL ending in .png can serve HTML, redirect, or track users.
            if (allow_images and url.scheme.lower() == 'https'
                    and trusted_host(url.hostname, TRUSTED_IMAGE_HOSTS)
                    and url.path.lower().endswith(('.png','.jpg','.jpeg'))):
                continue
        except ValueError:
            pass
        return True
    return False
