# WebATEM 0.3 and earlier kept the connection log in atem_control_atemcontrollog
# (label atem_control, no user column). Copy those rows into the new table so
# "Recent ATEMs" survives the upgrade. Only a table of that exact shape is
# read — a hosting platform's own atem_control table (it has a user column)
# is left alone.
import json

from django.db import migrations


def copy_legacy_rows(apps, schema_editor):
    connection = schema_editor.connection
    legacy = 'atem_control_atemcontrollog'
    with connection.cursor() as cursor:
        if legacy not in connection.introspection.table_names(cursor):
            return
        columns = {c.name for c in connection.introspection.get_table_description(cursor, legacy)}
        if 'user_id' in columns or not {'date', 'type', 'data'} <= columns:
            return
        cursor.execute(f'SELECT date, type, data FROM {legacy} ORDER BY date')
        rows = cursor.fetchall()
    ATEMControlLog = apps.get_model('webatem_atem', 'ATEMControlLog')
    for date, kind, data in rows:
        if isinstance(data, (str, bytes)):
            try:
                data = json.loads(data)
            except ValueError:
                data = {}
        ATEMControlLog.objects.create(date=date, type=kind, data=data or {})


class Migration(migrations.Migration):

    dependencies = [
        ('webatem_atem', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(copy_legacy_rows, migrations.RunPython.noop),
    ]
