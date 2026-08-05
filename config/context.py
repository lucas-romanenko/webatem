"""Template context: the app title shown in the navbar/drawer brand."""
from django.conf import settings


def app(request):
    return {'app_title': settings.APP_TITLE}
