"""Read-only Samsara adapter. Never logs the bearer token or response bodies."""
import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Authorization to a redirect destination.


def retry_delay(value, attempt):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, AttributeError):
            return min(30, 2 ** attempt) + random.random()


def page_parts(page):
    data, pagination = page['data'], page['pagination']
    cursor, more = pagination['endCursor'], pagination['hasNextPage']
    if not isinstance(data, list) or not isinstance(cursor, str) or type(more) is not bool:
        raise ValueError('Invalid response envelope')
    if more and not cursor:
        raise ValueError('Missing continuation cursor')
    return data, cursor, more


class Samsara:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())

    def get(self, path, params):
        request = Request('https://api.samsara.com' + path + '?' + urlencode(params),
                          headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'})
        for attempt in range(5):
            try:
                with self.opener.open(request, timeout=20) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code != 429 and not 500 <= exc.code < 600:
                    raise RuntimeError(f'Samsara HTTP {exc.code}; check token, scopes and parameters') from None
                delay = retry_delay(exc.headers.get('Retry-After'), attempt)
            except (URLError, TimeoutError):
                delay = retry_delay(None, attempt)
            if attempt == 4:
                raise RuntimeError('Samsara unavailable after five attempts')
            time.sleep(delay)

    def assignments(self, at, assignment_type, start=None):
        # Fixed timestamp across pages; publish only a complete snapshot.
        params = {'filterBy': 'vehicles', 'startTime': start or at, 'endTime': at,
                  'assignmentType': assignment_type}
        result, seen = [], set()
        while True:
            data, cursor, more = page_parts(self.get('/fleet/driver-vehicle-assignments', params))
            result.extend(data)
            if not more:
                return result
            if cursor in seen:
                raise ValueError('Assignment cursor did not progress')
            seen.add(cursor)
            params['after'] = cursor
            time.sleep(0.25)  # Stay below the documented assignment rate limit.
