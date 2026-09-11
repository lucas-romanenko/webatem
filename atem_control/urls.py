from django.urls import path

from atem_control.control import views as control_views
from atem_control.hyperdeck import views as hyperdeck_views
from atem_control.profile import views as profile_views
from atem_control.media_pool import views as media_pool_views

app_name = 'atem_control'

urlpatterns = [

    path('',                            control_views.atem_connect, name='connect'),
    path('control/',                    control_views.atem_control, name='control'),
    path('api/status/',                 control_views.atem_status, name='atem_status'),
    path('api/lookup-name/',            control_views.atem_lookup_name, name='atem_lookup_name'),
    path('api/discovered/',             control_views.atem_discovered, name='atem_discovered'),
    path('api/scan/',                   control_views.atem_scan, name='atem_scan'),
    path('device-info/',                control_views.atem_device_info, name='atem_device_info'),
    path('device-name/',                control_views.atem_set_device_name, name='atem_set_device_name'),
    path('media-pool-upload/',          media_pool_views.media_pool_upload, name='media_pool_upload'),
    path('profile/save_dialog_init/',   profile_views.profile_save_dialog_init,
         name='profile_save_dialog_init'),
    path('profile/save/',               profile_views.profile_save, name='profile_save'),
    path('profile/save/cancel/',        profile_views.profile_save_cancel,
         name='profile_save_cancel'),
    path('profile/load_xml/',           profile_views.profile_load_xml,
         name='profile_load_xml'),
    path('profile/load/',               profile_views.profile_load, name='profile_load'),
    path('hyperdeck/state/',            hyperdeck_views.hyperdeck_state, name='hyperdeck_state'),
    path('hyperdeck/status/',           hyperdeck_views.hyperdeck_status, name='hyperdeck_status'),
    path('hyperdeck/transport/',        hyperdeck_views.hyperdeck_transport, name='hyperdeck_transport'),
]
