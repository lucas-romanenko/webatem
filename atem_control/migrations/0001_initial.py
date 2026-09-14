# The app's history starts here, under its own label (webatem_atem). Earlier
# WebATEM releases kept this table under the label atem_control; 0002 brings
# those rows across.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ATEMControlLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='Date')),
                ('type', models.CharField(choices=[('connection', 'Connection'), ('disconnection', 'Disconnection')], db_index=True, max_length=20, verbose_name='Type')),
                ('data', models.JSONField(default=dict, verbose_name='Data')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='webatem_atem_logs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'ATEM Control Log',
                'verbose_name_plural': 'ATEM Control Logs',
                'ordering': ['-date'],
                'indexes': [
                    models.Index(fields=['type', '-date'], name='webatem_atem_type_date_idx'),
                    models.Index(fields=['user', '-date'], name='webatem_atem_user_date_idx'),
                ],
            },
        ),
    ]
