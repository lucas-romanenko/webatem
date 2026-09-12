from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path('', RedirectView.as_view(url='/atem/', permanent=False)),
    path('atem/', include('atem_control.urls')),
]
