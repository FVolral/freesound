# -*- coding: utf-8 -*-
# Generated by Django 1.11 on 2018-01-24 11:50
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0016_oldusername_created'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='num_pack_downloads',
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name='profile',
            name='num_sound_downloads',
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
    ]
