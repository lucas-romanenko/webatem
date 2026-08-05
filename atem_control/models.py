from django.db import models
from django.utils import timezone


class ATEMControlLog(models.Model):
    """Connect/disconnect audit trail. Drives the Connect page's
    "Recent ATEMs" quick-connect list."""

    LOG_TYPE_CHOICES = [
        ('connection', 'Connection'),
        ('disconnection', 'Disconnection'),
    ]

    date = models.DateTimeField('Date', default=timezone.now, db_index=True)
    type = models.CharField('Type', max_length=20, choices=LOG_TYPE_CHOICES, db_index=True)
    data = models.JSONField('Data', default=dict)

    class Meta:
        verbose_name = 'ATEM Control Log'
        verbose_name_plural = 'ATEM Control Logs'
        ordering = ['-date']
        indexes = [
            models.Index(fields=['type', '-date']),
        ]

    def __str__(self):
        ip_address = self.data.get('ip_address', 'Unknown')
        return f"{self.type} - {ip_address} - {self.date}"


def get_recent_atems(limit=5):
    """The last N unique ATEM IPs anyone connected to, most recent first.
    Returns dicts with 'ip_address', 'equipment_name', 'last_connected'."""
    seen_ips = {}
    for log in ATEMControlLog.objects.filter(type='connection').order_by('-date')[:200]:
        ip_address = log.data.get('ip_address')
        if ip_address and ip_address not in seen_ips:
            seen_ips[ip_address] = {
                'ip_address': ip_address,
                'equipment_name': log.data.get('equipment_name'),
                'last_connected': log.date,
            }
            if len(seen_ips) >= limit:
                break
    return list(seen_ips.values())
