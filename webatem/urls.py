from django.urls import include, path
from django.views.generic import RedirectView

from webatem import views

urlpatterns = [
    path('', RedirectView.as_view(url='/atem/', permanent=False)),
    path('atem/', include('atem_control.urls')),
    path('server/settings/', views.server_settings, name='server_settings'),
    path('launcher/', views.launcher_page, name='launcher'),
    path('server/quit/', views.server_quit, name='server_quit'),
]
