import mimetypes
from tempfile import SpooledTemporaryFile

from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils import timezone
from django.utils.deconstruct import deconstructible
from django.utils.encoding import force_bytes, smart_str

from storages.utils import clean_name, safe_join, setting

try:
    from urllib.parse import unquote_plus
except ImportError:
    from urllib import unquote_plus

try:
    from google.cloud.storage.client import Client
    from google.cloud.storage.blob import Blob
    from google.cloud.exceptions import NotFound
except ImportError:
    raise ImproperlyConfigured("Could not load Google Cloud Storage bindings.\n"
                               "See https://github.com/GoogleCloudPlatform/gcloud-python")


class GoogleCloudFile(File):
    def __init__(self, name, mode, storage):
        self.name = name
        self.mime_type = mimetypes.guess_type(name)[0]
        self._mode = mode
        self._storage = storage
        self.blob = storage.bucket.get_blob(name)
        if not self.blob and 'w' in mode:
            self.blob = Blob(self.name, storage.bucket)
        self._file = None
        self._is_dirty = False

    @property
    def size(self):
        return self.blob.size

    def _get_file(self):
        if self._file is None:
            self._file = SpooledTemporaryFile(
                max_size=self._storage.max_memory_size,
                suffix=".GSStorageFile",
                dir=setting("FILE_UPLOAD_TEMP_DIR", None)
            )
            if 'r' in self._mode:
                self._is_dirty = False
                self.blob.download_to_file(self._file)
                self._file.seek(0)
        return self._file

    def _set_file(self, value):
        self._file = value

    file = property(_get_file, _set_file)

    def read(self, num_bytes=None):
        if 'r' not in self._mode:
            raise AttributeError("File was not opened in read mode.")

        if num_bytes is None:
            num_bytes = -1

        return super(GoogleCloudFile, self).read(num_bytes)

    def write(self, content):
        if 'w' not in self._mode:
            raise AttributeError("File was not opened in write mode.")
        self._is_dirty = True
        return super(GoogleCloudFile, self).write(force_bytes(content))

    def close(self):
        if self._file is not None:
            if self._is_dirty:
                self.file.seek(0)
                self.blob.upload_from_file(self.file, content_type=self.mime_type)
            self._file.close()
            self._file = None


@deconstructible
class GoogleCloudStorage(Storage):
    project_id = setting('GS_PROJECT_ID', None)
    credentials = setting('GS_CREDENTIALS', None)
    bucket_name = setting('GS_BUCKET_NAME', None)
    auto_create_bucket = setting('GS_AUTO_CREATE_BUCKET', False)
    auto_create_acl = setting('GS_AUTO_CREATE_ACL', 'projectPrivate')
    file_name_charset = setting('GS_FILE_NAME_CHARSET', 'utf-8')
    file_overwrite = setting('GS_FILE_OVERWRITE', True)
    # The max amount of memory a returned file can take up before being
    # rolled over into a temporary file on disk. Default is 0: Do not roll over.
    max_memory_size = setting('GS_MAX_MEMORY_SIZE', 0)

    def __init__(self, **settings):
        # check if some of the settings we've provided as class attributes
        # need to be overwritten with values passed in here
        for name, value in settings.items():
            if hasattr(self, name):
                setattr(self, name, value)

        self._bucket = None
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = Client(
                project=self.project_id,
                credentials=self.credentials
            )
        return self._client

    @property
    def bucket(self):
        if self._bucket is None:
            self._bucket = self._get_or_create_bucket(self.bucket_name)
        return self._bucket

    def _get_or_create_bucket(self, name):
        """
        Retrieves a bucket if it exists, otherwise creates it.
        """
        try:
            return self.client.get_bucket(name)
        except NotFound:
            if self.auto_create_bucket:
                bucket = self.client.create_bucket(name)
                bucket.acl.save_predefined(self.auto_create_acl)
                return bucket
            raise ImproperlyConfigured("Bucket %s does not exist. Buckets "
                                       "can be automatically created by "
                                       "setting GS_AUTO_CREATE_BUCKET to "
                                       "``True``." % name)

    def _normalize_name(self, name):
        """
        Normalizes the name so that paths like /path/to/ignored/../something.txt
        and ./file.txt work.  Note that clean_name adds ./ to some paths so
        they need to be fixed here.
        """
        return safe_join('', name)

    def _encode_name(self, name):
        return smart_str(name, encoding=self.file_name_charset)

    def _open(self, name, mode='rb'):
        name = self._normalize_name(clean_name(name))
        file_object = GoogleCloudFile(name, mode, self)
        if not file_object.blob:
            raise IOError(u'File does not exist: %s' % name)
        return file_object

    def _save(self, name, content):
        cleaned_name = clean_name(name)
        name = self._normalize_name(cleaned_name)

        content.name = cleaned_name
        encoded_name = self._encode_name(name)
        file = GoogleCloudFile(encoded_name, 'rw', self)
        file.blob.upload_from_file(content, size=content.size,
                                   content_type=file.mime_type)
        return cleaned_name

    def delete(self, name):
        name = self._normalize_name(clean_name(name))
        self.bucket.delete_blob(self._encode_name(name))

    def exists(self, name):
        if not name:  # root element aka the bucket
            try:
                self.bucket
                return True
            except ImproperlyConfigured:
                return False

        name = self._normalize_name(clean_name(name))
        return bool(self.bucket.get_blob(self._encode_name(name)))

    def listdir(self, name):
        name = self._normalize_name(clean_name(name))
        # for the bucket.list and logic below name needs to end in /
        # But for the root path "" we leave it as an empty string
        if name and not name.endswith('/'):
            name += '/'

        files_list = list(self.bucket.list_blobs(prefix=self._encode_name(name)))
        files = []
        dirs = set()

        base_parts = name.split("/")[:-1]
        for item in files_list:
            parts = item.name.split("/")
            parts = parts[len(base_parts):]
            if len(parts) == 1 and parts[0]:
                # File
                files.append(parts[0])
            elif len(parts) > 1 and parts[0]:
                # Directory
                dirs.add(parts[0])
        return list(dirs), files

    def _get_blob(self, name):
        # Wrap google.cloud.storage's blob to raise if the file doesn't exist
        blob = self.bucket.get_blob(name)

        if blob is None:
            raise NotFound(u'File does not exist: {}'.format(name))

        return blob

    def size(self, name):
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        return blob.size

    def modified_time(self, name):
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        return timezone.make_naive(blob.updated)

    def get_modified_time(self, name):
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        updated = blob.updated
        return updated if setting('USE_TZ') else timezone.make_naive(updated)

    def url(self, name):
        # Preserve the trailing slash after normalizing the path.
        name = self._normalize_name(clean_name(name))
        blob = self._get_blob(self._encode_name(name))
        return unquote_plus(blob.public_url)

    def get_available_name(self, name, max_length=None):
        if self.file_overwrite:
            name = clean_name(name)
            return name
        return super(GoogleCloudStorage, self).get_available_name(name, max_length)
