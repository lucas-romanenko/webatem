from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atem_control', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Switcher',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('ip', models.GenericIPAddressField(unique=True)),
                ('notes', models.CharField(blank=True, default='', max_length=255)),
            ],
            options={
                'verbose_name': 'Switcher',
                'verbose_name_plural': 'Switchers',
                'ordering': ['name'],
            },
        ),
    ]
