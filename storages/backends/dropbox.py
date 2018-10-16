# Dropbox storage class for Django pluggable storage system.
# Author: Anthony Monthe <anthony.monthe@gmail.com>
# License: BSD
#
# Usage:
#
# Add below to settings.py:
# DROPBOX_OAUTH2_TOKEN = 'YourOauthToken'
# DROPBOX_ROOT_PATH = '/dir/'

from __future__ import absolute_import

from datetime import datetime
from shutil import copyfileobj
from tempfile import SpooledTemporaryFile

from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils._os import safe_join
from django.utils.deconstruct import deconstructible
from dropbox import Dropbox
from dropbox.exceptions import ApiError
from dropbox.files import CommitInfo, UploadSessionCursor

from storages.utils import setting

DATE_FORMAT = '%a, %d %b %Y %X +0000'


class DropBoxStorageException(Exception):
    pass


class DropBoxFile(File):
    def __init__(self, name, storage):
        self.name = name
        self._storage = storage

    @property
    def file(self):
        if not hasattr(self, '_file'):
            response = self._storage.client.files_download(self.name)
            self._file = SpooledTemporaryFile()
            copyfileobj(response, self._file)
            self._file.seek(0)
        return self._file


@deconstructible
class DropBoxStorage(Storage):
    """DropBox Storage class for Django pluggable storage system."""

    CHUNK_SIZE = 4 * 1024 * 1024

    def __init__(self, oauth2_access_token=None, root_path=None):
        oauth2_access_token = oauth2_access_token or setting('DROPBOX_OAUTH2_TOKEN')
        self.root_path = root_path or setting('DROPBOX_ROOT_PATH', '/')
        if oauth2_access_token is None:
            raise ImproperlyConfigured("You must configure a token auth at"
                                       "'settings.DROPBOX_OAUTH2_TOKEN'.")
        self.client = Dropbox(oauth2_access_token)

    def _full_path(self, name):
        if name == '/':
            name = ''
        return safe_join(self.root_path, name).replace('\\', '/')

    def delete(self, name):
        self.client.files_delete(self._full_path(name))

    def exists(self, name):
        try:
            return bool(self.client.files_get_metadata(self._full_path(name)))
        except ApiError:
            return False

    def listdir(self, path):
        directories, files = [], []
        full_path = self._full_path(path)
        metadata = self.client.files_get_metadata(full_path)
        for entry in metadata['contents']:
            entry['path'] = entry['path'].replace(full_path, '', 1)
            entry['path'] = entry['path'].replace('/', '', 1)
            if entry['is_dir']:
                directories.append(entry['path'])
            else:
                files.append(entry['path'])
        return directories, files

    def size(self, name):
        metadata = self.client.files_get_metadata(self._full_path(name))
        return metadata['bytes']

    def modified_time(self, name):
        metadata = self.client.files_get_metadata(self._full_path(name))
        mod_time = datetime.strptime(metadata['modified'], DATE_FORMAT)
        return mod_time

    def accessed_time(self, name):
        metadata = self.client.files_get_metadata(self._full_path(name))
        acc_time = datetime.strptime(metadata['client_mtime'], DATE_FORMAT)
        return acc_time

    def url(self, name):
        media = self.client.files_get_temporary_link(self._full_path(name))
        return media.link

    def _open(self, name, mode='rb'):
        remote_file = DropBoxFile(self._full_path(name), self)
        return remote_file

    def _save(self, name, content):
        content.open()
        if content.size <= self.CHUNK_SIZE:
            self.client.files_upload(content.read(), self._full_path(name))
        else:
            self._chunked_upload(content, self._full_path(name))
        content.close()
        return name

    def _chunked_upload(self, content, dest_path):
        upload_session = self.client.files_upload_session_start(
            content.read(self.CHUNK_SIZE)
        )
        cursor = UploadSessionCursor(
            session_id=upload_session.session_id,
            offset=content.tell()
        )
        commit = CommitInfo(path=dest_path)

        while content.tell() < content.size:
            if (content.size - content.tell()) <= self.CHUNK_SIZE:
                self.client.files_upload_session_finish(
                    content.read(self.CHUNK_SIZE), cursor, commit
                )
            else:
                self.client.files_upload_session_append_v2(
                    content.read(self.CHUNK_SIZE), cursor
                )
                cursor.offset = content.tell()


@deconstructible
class DropBoxTeamStorage(DropBoxStorage):
    """
    Dropbox Team Storage class for using with Team accounts
    When working with Dropbox Teams (part of the Business API), the API is
    different: when using the team token, you cannot access the folders without
    becoming a user first.  On top of that, without the Team folder namespace,
    you can only access the users personal folder, not the team folder.
    You need to use the header method to set the root path.  You also need
    the id of an admin user and your app needs to have the
    "Team member file access" permission for this to work
    Caveat: If you use the namespace id as part of the path (ns:xxxx/path)
    instead of using the Dropbox-API-Path-Root header, no path info is returned
    in the file_list_folder responses - unexpected to say the least
    """

    def __init__(self, oauth2_access_token=None, root_path=None, headers=None,
                 team_namespace=None, team_admin=None):
        oauth2_access_token = oauth2_access_token or \
            setting('DROPBOX_OAUTH2_TOKEN')
        team_namespace = team_namespace or setting('DROPBOX_TEAM_NAMESPACE')
        team_admin = team_admin or setting('DROPBOX_TEAM_ADMIN')
        self.root_path = root_path or setting('DROPBOX_ROOT_PATH', '/')
        if oauth2_access_token is None:
            raise ImproperlyConfigured("You must configure a token auth at"
                                       "'settings.DROPBOX_OAUTH2_TOKEN'.")
        if team_namespace is None:
            raise ImproperlyConfigured("You must configure a namespace at"
                                       "'settings.DROPBOX_TEAM_NAMESPACE'.")
        if team_admin is None:
            raise ImproperlyConfigured("You must configure an admin at"
                                       "'settings.DROPBOX_TEAM_ADMIN'.")

        # add Dropbox-API-Path-Root header using the namespace syntax
        self.client = DropboxTeam(
            oauth2_access_token,
            headers={
                'Dropbox-API-Path-Root':
                    '{".tag": "namespace_id", "namespace_id": "%s"}' %
                    team_namespace
            }
        ).as_admin("dbmid:%s" % team_admin)  # and become a user
