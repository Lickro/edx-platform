"""
Script for removing all redundant Mac OS metadata files (with filename ".DS_Store"
or with filename which starts with "._") for all courses
"""
import logging

from django.core.management.base import BaseCommand

from xmodule.contentstore.django import contentstore

log = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Remove all Mac OS related redundant files for all courses in contentstore
    """
    help = 'Remove all Mac OS related redundant file/files for all courses in contentstore'

    def handle(self, *args, **options):
        """
        Execute the command
        """
        content_store = contentstore()
        success = False

        log.info(u"-" * 80)
        log.info(u"Cleaning up assets for all courses")
        try:
            # Remove all redundant Mac OS metadata files
            assets_deleted = content_store.remove_redundant_content_for_courses()
            success = True
        except Exception as err:
            log.info(u"=" * 30 + u"> failed to cleanup")
            log.info(u"Error:")
            log.info(err)

        if success:
            log.info(u"=" * 80)
            log.info(u"Total number of assets deleted: {0}".format(assets_deleted))
