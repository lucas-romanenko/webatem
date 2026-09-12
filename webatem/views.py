"""Project-level views: the Server settings endpoint the connect page's
dialog talks to (the app views live in atem_control)."""
import json

from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from webatem import server as srv


@require_http_methods(['GET', 'POST'])
def server_settings(request):
    """GET: the current listen address, the addresses available, and what
    can be changed live. POST {host, port[, autostart]}: save; if the
    launcher is hosting us and the address changed, restart on it."""
    if request.method == 'GET':
        return JsonResponse(srv.describe())
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        return HttpResponseBadRequest('invalid json')
    try:
        host, port = srv.validate(body.get('host'), body.get('port'))
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)

    before = tuple(srv.runtime.current())     # read BEFORE saving: without a controller the file IS the current value
    result = {'saved': True, 'host': host, 'port': port}
    if 'autostart' in body and srv.runtime.autostart_available():
        try:
            srv.runtime.set_autostart(bool(body['autostart']))
            result['autostart'] = srv.runtime.autostart_enabled()
        except Exception as e:  # noqa: BLE001 — a login-entry failure is reported, not fatal
            result['autostart_error'] = str(e)
    srv.save(host, port, body.get('start_minimized') if 'start_minimized' in body else None)

    changed = (host, port) != before
    if changed and srv.runtime.restart_available():
        result['restarting'] = True
        result['url'] = srv.url_for(host, port, request.get_host())
        srv.runtime.request_restart(host, port)
    elif changed:
        result['restarting'] = False
        result['note'] = srv.NO_RESTART_NOTE
    return JsonResponse(result)


@require_GET
def launcher_page(request):
    """The launcher window (Companion-style): Running + the address, the
    interface and port, Start minimized, Run at login, Launch GUI / Hide /
    Quit. Shown inside the desktop launcher's native window, which injects
    window.pywebview for the three buttons; in a plain browser the page is
    the same settings surface without them."""
    try:
        from importlib.metadata import version
        ver = version('webatem')
    except Exception:  # noqa: BLE001 — a checkout without metadata
        ver = 'dev'
    return render(request, 'launcher.html', {'version': ver})


@require_POST
def server_quit(request):
    """The launcher window's Quit: stop the whole launcher (tray included).
    Only meaningful under the launcher; plain uvicorn answers 409."""
    if not srv.runtime.quit_available():
        return JsonResponse({'error': 'not running under the launcher'}, status=409)
    srv.runtime.quit()
    return JsonResponse({'ok': True})
