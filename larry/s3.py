import boto3
from boto3.s3.transfer import TransferConfig
import os
import re
import uuid
import json
import inspect
from io import StringIO, BytesIO
from tempfile import TemporaryFile
from collections import Mapping

import larry.core
from larry import utils
from larry import sts
from larry.types import Types
from larry import ClientError
from larry.core import ResourceWrapper
from larry.core import attach_exception_handler
from larry.core import resolve_client
from urllib import parse
from urllib import request
from zipfile import ZipFile
from enum import Enum
from functools import wraps

# Local S3 resource object
__resource = None
# A local instance of the boto3 session to use
__session = boto3.session.Session()

URI_REGEX = re.compile("^[sS]3://([a-z0-9.-]{3,})/?(.*)")

ACL_PRIVATE = 'private'
ACL_PUBLIC_READ = 'public-read'
ACL_PUBLIC_READ_WRITE = 'public-read-write'
ACL_AUTHENTICATED_READ = 'authenticated-read'
ACL_AWS_EXEC_READ = 'aws-exec-read'
ACL_BUCKET_OWNER_READ = 'bucket-owner-read'
ACL_BUCKET_OWNER_FULL_CONTROL = 'bucket-owner-full-control'

CLASS_STANDARD = 'STANDARD'
CLASS_REDUCED_REDUNDANCY = 'REDUCED_REDUNDANCY'
CLASS_STANDARD_IA = 'STANDARD_IA'
CLASS_ONEZONE_IA = 'ONEZONE_IA'
CLASS_INTELLIGENT_TIERING = 'INTELLIGENT_TIERING'
CLASS_GLACIER = 'GLACIER'
CLASS_DEEP_ARCHIVE = 'DEEP_ARCHIVE'


def __getattr__(name):
    if name == 'resource':
        return __resource
    elif name == 'session':
        return __session
    elif name == 'client':
        return __resource.meta.client
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def _get_resource(): return __resource


def set_session(aws_access_key_id=None,
                aws_secret_access_key=None,
                aws__session_token=None,
                region_name=None,
                profile_name=None,
                boto_session=None):
    """
    Sets the boto3 session for this module to use a specified configuration state.

    :param aws_access_key_id: AWS access key ID
    :param aws_secret_access_key: AWS secret access key
    :param aws__session_token: AWS temporary session token
    :param region_name: Default region when creating new connections
    :param profile_name: The name of a profile to use
    :param boto_session: An existing session to use
    :return: None
    """
    global __session, __resource
    __session = boto_session if boto_session is not None else boto3.session.Session(
        **larry.core.copy_non_null_keys(locals()))
    sts.set_session(boto_session=__session)
    __resource = __session.resource('s3')


def _resolve_location(require_bucket=True, require_key=False, key_arg='key', allow_multiple=False):
    """
    Builds a function decorator that will reconcile methods of passing an object to a function. Allows functions
    to accept bucket/key pairs or URIs, either as named or location parameters.

    :param require_bucket: Require that the function call includes a bucket parameter
    :param require_key: Require that the function call includes a key parameter
    :param key_arg: The function argument that contains the key value
    :param allow_multiple: Allow the function to accept a list of keys or URIs
    :return: The decorator
    """
    def decorate(func):
        spec = inspect.getfullargspec(func)
        offset = len(spec.args)

        @wraps(func)
        def resolve_location(*args, **kwargs):
            location = args[offset:]
            uri, bucket, key = (None, None, None)
            if kwargs.get('uri') is None and kwargs.get('bucket') is None and kwargs.get(key_arg) is None:
                if len(location) == 0:
                    raise TypeError('A location must be specified')
                if len(location) > 2:
                    raise TypeError('Too many location values')
                if len(location) == 1:
                    if isinstance(location[0], list) and location[0][0].startswith('s3:'):
                        uri = location[0]
                    elif isinstance(location[0], str) and location[0].startswith('s3:'):
                        uri = location[0]
                    else:
                        bucket = location[0]
                else:
                    (bucket, key) = location
            elif len(location) > 0:
                raise TypeError('Both positional location and ' + key_arg + ' values are present')

            if 'uri' in kwargs and kwargs['uri'] is not None:
                uri = kwargs['uri']
            if 'bucket' in kwargs and kwargs['bucket'] is not None:
                bucket = kwargs['bucket']
            if key_arg in kwargs and kwargs[key_arg] is not None:
                key = kwargs[key_arg]

            if uri:
                if isinstance(uri, list) and len(uri) > 0:
                    if not allow_multiple:
                        raise TypeError('You cannot provide a list of URIs for {}'.format(func.__name__))
                    pairs = [decompose_uri(u) for u in uri]
                    key = []
                    bucket = pairs[0][0]
                    for pair in pairs:
                        key.append(pair[1])
                        if pair[0] != bucket:
                            raise TypeError('Multiple values for bucket are not allowed')
                else:
                    (bucket, key) = decompose_uri(uri)
                    if bucket is None:
                        raise TypeError('Invalid S3 URI')

            if isinstance(key, list) and not allow_multiple:
                raise TypeError('You cannot provide a list of keys for {}'.format(func.__name__))

            if require_bucket and (bucket is None or len(bucket) == 0):
                raise TypeError('A bucket must be provided')
            if require_key and (key is None or len(key) == 0):
                raise TypeError('A key must be provided')

            kwargs['bucket'] = bucket
            kwargs[key_arg] = key
            return func(*args, **kwargs)

        return resolve_location

    return decorate


class Object(ResourceWrapper):
    """
    Wraps the boto3 S3
    `Object <https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#object>`_
    resource with helper functions to make it easier to interact with objects
    and access additional attributes.

    .. code-block:: python

        import larry as lry
        obj = lry.s3.Object('bucket_name', 'key')

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket
    :param key: The key of the object
    :param uri: An s3:// path containing the bucket and key of the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    """

    @_resolve_location(require_key=True)
    def __init__(self, *location, bucket=None, key=None, uri=None, s3_resource=None):
        super().__init__(Bucket(bucket, s3_resource=s3_resource).Object(key=key))

    @property
    @attach_exception_handler
    def tags(self):
        """
        Returns dict containing a key/value pair for the tags that have been attached to the object.
        """
        tags = self.meta.client.get_object_tagging(Bucket=self.bucket_name, Key=self.key)._get('TagSet', [])
        return {pair['Key']: pair['Value'] for pair in tags}

    @property
    @attach_exception_handler
    def exists(self):
        """
        Attempts to load header information for the S3 object and returns true if it exists, false otherwise.
        """
        try:
            self.load()
        except ClientError as e:
            if e.code == "404":
                return False
            else:
                raise e
        return True

    @attach_exception_handler
    def set_acl(self, acl):
        """
        Assigns the provided ACL to the object.
        """
        self.meta.client.put_object_acl(Bucket=self.bucket_name, Key=self.key, ACL=acl)

    def make_public(self):
        """
        Assigns a public-read ACL to the object to allow anyone to access it.
        """
        self.set_acl(ACL_PUBLIC_READ)
        return self.url

    @property
    def url(self):
        """
        Returns the public URL of the object (assuming permissions have been set appropriately).
        """
        return _object_url(self.bucket_name, self.key)


class Bucket(ResourceWrapper):
    """
    Wraps the boto3 S3
    `Bucket <https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#bucket>`_
    resource with helper functions to make it easier to interact with buckets
    and access additional attributes.

    .. code-block:: python

        import larry as lry
        bucket = lry.s3.Bucket('bucket_name')

    :param bucket: The S3 bucket
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    """

    @resolve_client(_get_resource, 's3_resource')
    def __init__(self, bucket, s3_resource=None):
        super().__init__(s3_resource.Bucket(bucket))

    @property
    @attach_exception_handler
    def exists(self):
        """
        Will attempt to retrieve information for the S3 bucket and returns true if it exists, false otherwise.
        """
        try:
            create_date = self.creation_date
            return create_date is not None
        except ClientError as e:
            if e.code == "404":
                return False
            else:
                raise e

    @property
    def url(self):
        """
        Returns the public URL of the bucket (assuming permissions have been set appropriately).
        """
        return _bucket_url(self.bucket_name)


@_resolve_location(require_key=True, allow_multiple=True)
def delete(*location, bucket=None, key=None, uri=None, s3_resource=None):
    """
    Deletes the object defined by the bucket/key pair or uri.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket
    :param key: The key of the object, this can be a single str value or a list of keys to delete
    :param uri: An s3:// path containing the bucket and key of the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    """
    if isinstance(key, list):
        Bucket(bucket=bucket, s3_resource=s3_resource).delete_objects(Delete={'Objects': [{'Key': k} for k in key], 'Quiet': True})
    else:
        Object(bucket=bucket, key=key, s3_resource=s3_resource).delete()


@_resolve_location(require_key=True)
def _get(*location, bucket=None, key=None, uri=None, s3_resource=None):
    """
    Performs a 'get' of the object defined by the bucket/key pair or uri.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: Dict containing the Body of the object and associated attributes
    """
    return Object(bucket=bucket, key=key, s3_resource=s3_resource).get()


@_resolve_location(require_key=True)
def get_size(*location, bucket=None, key=None, uri=None, s3_resource=None):
    """
    Returns the number of bytes (content_length) in an S3 object.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: Size in bytes
    """
    return Object(bucket=bucket, key=key, s3_resource=s3_resource).content_length


@_resolve_location(require_key=True)
def read(*location, bucket=None, key=None, uri=None, byte_count=None, s3_resource=None):
    """
    Retrieves the contents of an S3 object

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param byte_count: The max number of bytes to read from the object. All data is read if omitted.
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The bytes contained in the object
    """
    return _get(bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)['Body'].read(byte_count)


@_resolve_location(require_key=True)
def read_as(type_, *location, bucket=None, key=None, uri=None, encoding='utf-8', s3_resource=None):
    """
    Reads in the s3 object defined by the bucket/key pair or uri and loads the
    contents into an object of the specified type.

    .. code-block:: python

        import larry as lry
        from larry.types import Types
        np_array = lry.s3.read_as(Types.NP_ARRAY, 'my-bucket', 'my-key')

    :param type_: A value from larry.types.Types to indicate how to read in the data
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param encoding: The charset to use when decoding the object bytes, utf-8 by default
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: An object representation of the data in S3
    """
    if type_ == Types.NP_ARRAY:
        try:
            import numpy as np
            with TemporaryFile() as fp:
                download(fp, bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)
                fp.seek(0)
                return np.fromfile(fp)
        except ImportError as e:
            # Simply raise the ImportError to let the user know this requires Numpy to function
            raise e
    else:
        objct = read(bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)

        # TODO: Would a handler or local constant be a better idea here?
        if type_ == Types.DICT:
            return json.loads(objct.decode(encoding), object_hook=utils.JSONDecoder)
        elif type_ == Types.STRING:
            return objct.decode(encoding)
        elif type_ == Types.PILLOW_IMAGE:
            try:
                from PIL import Image
                return Image.open(BytesIO(objct))
            except ImportError as e:
                # Simply raise the ImportError to let the user know this requires Pillow to function
                raise e
        else:
            raise TypeError('Unhandled type')


@_resolve_location(require_key=True)
def read_list_as(o_type, *location, bucket=None, key=None, uri=None, encoding='utf-8', newline='\n', s3_resource=None):
    """
    Reads in the s3 object defined by the bucket/key pair or uri, decodes it to a string, and
    splits it into lines. Returns an array containing the contents of each line in the specified format.

    :param o_type: A value defined in larry.types to load the data using
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param encoding: The charset to use when decoding the object bytes, utf-8 by default
    :param newline: The line separator to use when reading in the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: An object representation of the data in S3
    """
    objct = read(bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)
    lines = objct.decode(encoding).split(newline)
    records = []
    for line in lines:
        if len(line) > 0:
            # TODO: Would a handler or local constant be a better idea here?
            if o_type == Types.DICT:
                records.append(json.loads(line, object_hook=utils.JSONDecoder))
            elif o_type == Types.STRING:
                records.append(line)
            else:
                raise TypeError('Unhandled type')
    return records


@_resolve_location(require_key=True)
def read_iter_as(o_type, *location, bucket=None, key=None, uri=None, encoding='utf-8', newline='\n', s3_resource=None):
    """
    Reads in the s3 object defined by the bucket/key pair or uri, decodes it to a string, and
    splits it into lines. Returns an iterator containing the contents of each line.

    :param o_type: A value defined in larry.types to load the data using
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param encoding: The charset to use when decoding the object bytes, utf-8 by default
    :param newline: The line separator to use when reading in the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: An object representation of the data in S3
    """

    objct = read(bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)
    lines = objct.decode(encoding).split(newline)
    for line in lines:
        if len(line) > 0:
            # TODO: Would a handler or local constant be a better idea here?
            if o_type == Types.DICT:
                yield json.loads(line, object_hook=utils.JSONDecoder)
            elif o_type == Types.STRING:
                yield line
            else:
                raise TypeError('Unhandled type')


@_resolve_location(require_key=True)
def read_dict(*location, bucket=None, key=None, uri=None, encoding='utf-8', s3_resource=None):
    """
    Reads in the s3 object defined by the bucket/key pair or uri and
    loads the json contents into a dict.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param encoding: The charset to use when decoding the object bytes, utf-8 by default
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: A dict representation of the json contained in the object
    """
    return read_as(Types.DICT, bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)


@_resolve_location(require_key=True)
def read_str(*location, bucket=None, key=None, uri=None, encoding='utf-8', s3_resource=None):
    """
    Reads in the s3 object defined by the bucket/key pair or uri and
    decodes it to text.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param encoding: The charset to use when decoding the object bytes, utf-8 by default
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The contents of the object as a string
    """
    return read_as(Types.STRING, bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)


@_resolve_location(require_key=True)
def read_list_of_dict(*location, bucket=None, key=None, uri=None, encoding='utf-8', newline='\n', s3_resource=None):
    """
    Reads in the s3 object defined by the bucket/key pair or uri and
    loads the JSON Lines data into a list of dict objects.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param encoding: The charset to use when decoding the object bytes, utf-8 by default
    :param newline: The line separator to use when reading in the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The contents of the object as a list of dict objects
    """
    return read_list_as(Types.DICT, bucket=bucket, key=key, uri=uri,
                        encoding=encoding, newline=newline, s3_resource=s3_resource)


@_resolve_location(require_key=True)
def read_list_of_str(*location, bucket=None, key=None, uri=None, encoding='utf-8', newline='\n', s3_resource=None):
    """
    Reads in the s3 object defined by the bucket/key pair or uri and
    loads the JSON Lines data into a list of dict objects.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param encoding: The charset to use when decoding the object bytes, utf-8 by default
    :param newline: The line separator to use when reading in the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The contents of the object as a list of dict objects
    """
    return read_list_as(Types.STRING, bucket=bucket, key=key, uri=uri,
                        encoding=encoding, newline=newline, s3_resource=s3_resource)


def __write(body, bucket=None, key=None, uri=None, acl=None, content_type=None, content_encoding=None,
            content_language=None, content_length=None, metadata=None, sse=None, storage_class=None, 
            tags=None, s3_resource=None):
    """
    Write an object to the bucket/key pair or uri.

    :param body: Data to write
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param acl: The canned ACL to apply to the object
    :param content_type: A standard MIME type describing the format of the object data
    :param content_encoding: Specifies what content encodings have been applied to the object and thus what decoding
    mechanisms must be applied to obtain the media-type referenced by the Content-Type header field.
    :param content_language: The language the content is in.
    :param content_length: Size of the body in bytes.
    :param metadata: A map of metadata to store with the object in S3.
    :param sse: The server-side encryption algorithm used when storing this object in Amazon S3.
    :param storage_class: The S3 storage class to store the object in.
    :param tags: The tag-set for the object. Can be either a dict or url encoded key/value string.
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The URI of the object written to S3
    """
    params = larry.core.map_parameters(locals(), {
        'acl': 'ACL',
        'body': 'Body',
        'content_encoding': 'ContentEncoding',
        'content_language': 'ContentLanguage',
        'content_length': 'ContentLength',
        'content_type': 'ContentType',
        'metadata': 'Metadata',
        'sse': 'ServerSideEncryption',
        'storage_class': 'StorageClass',
    })
    if tags:
        params['Tagging'] = parse.urlencode(tags) if isinstance(tags, Mapping) else tags

    Object(bucket=bucket, key=key, s3_resource=s3_resource).put(**params)
    return compose_uri(bucket, key)


__extension_types = {
    'css': 'text/css',
    'html': 'text/html',
    'xhtml': 'text/html',
    'htm': 'text/html',
    'xml': 'text/xml',
    'csv': 'text/csv',
    'txt': 'text/plain',
    'png': 'image/png',
    'jpeg': 'image/jpeg',
    'jpg': 'image/jpeg',
    'gif': 'image/gif',
    'jsonl': 'application/x-jsonlines',
    'json': 'application/json',
    'js': 'application/javascript',
    'zip': 'application/zip',
    'pdf': 'application/pdf',
    'sql': 'application/sql',
    'tiff': 'image/tiff',
    'tif': 'image/tiff',
    'webp': 'image/webp',
    'bmp': 'image/bmp',
    'ico': 'image/vnd.microsoft.icon',
    'svg': 'image/svg+xml'
}

__content_type_to_pillow_format = {
    'image/png': 'PNG',
    'image/jpeg': 'JPEG',
    'image/gif': 'GIF',
    'image/tiff': 'TIFF',
    'image/webp': 'WebP',
    'image/bmp': 'BMP',
    'image/vnd.microsoft.icon': 'ICO'
}


@_resolve_location(require_key=True)
def write_as(value, o_type, *location, bucket=None, key=None, uri=None, acl=None, newline='\n', delimiter=',',
             columns=None, headers=None, content_type=None, content_encoding=None, content_language=None,
             content_length=None, metadata=None, sse=None, storage_class=None,  tags=None,
             s3_resource=None):
    """
    Write an object to the bucket/key pair (or uri), converting the python
    object to an appropriate format to write to file.

    :param value: Object to write to S3
    :param o_type: A value defined in larry.types to write the data using
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param newline: Character(s) to use as a newline for list objects
    :param delimiter: Column delimiter to use, ',' by default
    :param columns: The columns to write out from the source rows, dict keys or list indexes
    :param headers: Headers to add to the output
    :param acl: The canned ACL to apply to the object
    :param content_type: Content type to apply to the file, if not present a suggested type will be applied
    :param content_encoding: Specifies what content encodings have been applied to the object and thus what decoding
        mechanisms must be applied to obtain the media-type referenced by the Content-Type header field.
    :param content_language: The language the content is in.
    :param content_length: Size of the body in bytes.
    :param metadata: A map of metadata to store with the object in S3.
    :param sse: The server-side encryption algorithm used when storing this object in Amazon S3.
    :param storage_class: The S3 storage class to store the object in.
    :param tags: The tag-set for the object. Can be either a dict or url encoded key/value string.
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The URI of the object written to S3
    """
    extension = key.split('.')[-1]
    if o_type == Types.STRING:
        if content_type is None:
            content_type = __extension_types.get(extension, 'text/plain')
        objct = value
    elif o_type == Types.DICT:
        if content_type is None:
            content_type = 'application/json'
        objct = json.dumps(value, cls=utils.JSONEncoder)
    elif o_type == Types.PILLOW_IMAGE:
        objct = BytesIO()
        fmt = value.format if hasattr(value, 'format') and value.format is not None else 'PNG'
        value.save(objct, fmt)
        objct.seek(0)
        if content_type is None:
            content_type = __extension_types.get(extension, __extension_types.get(fmt.lower(), 'text/plain'))
    elif o_type == Types.JSON_LINES:
        if content_type is None:
            content_type = __extension_types.get(extension, 'text/plain')
        buff = StringIO()
        for row in value:
            buff.write(json.dumps(row, cls=utils.JSONEncoder) + newline)
        objct = buff.getvalue()
    elif o_type == Types.DELIMITED:
        if content_type is None:
            content_type = __extension_types.get(extension, 'text/plain')
        buff = StringIO()
        # empty
        if value is None or len(value) == 0:
            if headers:
                buff.write(_array_to_string(headers, delimiter) + newline)
            buff.write('')

        # list
        elif isinstance(value[0], list):
            indices = columns if columns else None
            if headers:
                buff.write(_array_to_string(headers, delimiter) + newline)
            for row in value:
                buff.write(_array_to_string(row, delimiter, indices) + newline)

        # dict
        elif isinstance(value[0], Mapping):
            keys = columns if columns else value[0].keys()
            buff.write(_array_to_string(headers if headers else keys, delimiter) + newline)

            for row in value:
                line = ''
                for i, k in enumerate(keys):
                    value = '' if row._get(k) is None else str(row._get(k))
                    line = value if i == 0 else line + delimiter + value
                buff.write(line + newline)

        # string
        elif isinstance(value[0], str):
            buff.writelines(value)
        else:
            raise TypeError('Invalid input')
        objct = buff.getvalue()
    else:
        raise TypeError('Unhandled type')
    return __write(objct, bucket=bucket, key=key, uri=uri, acl=acl, content_type=content_type,
                   content_encoding=content_encoding, content_language=content_language, content_length=content_length,
                   metadata=metadata, sse=sse, storage_class=storage_class, tags=tags,
                   s3_resource=s3_resource)


@_resolve_location(require_key=True)
def write_object(value, *location, bucket=None, key=None, uri=None, newline='\n', acl=None, content_type=None,
                 content_encoding=None, content_language=None, content_length=None, metadata=None, sse=None,
                 storage_class=None,  tags=None, s3_resource=None, **params):
    return write(value, bucket=bucket, key=key, uri=uri, newline=newline, acl=acl, content_type=content_type,
                 content_encoding=content_encoding, content_language=content_language, content_length=content_length,
                 metadata=metadata, sse=sse, storage_class=storage_class, tags=tags, s3_resource=s3_resource, **params)


def __value_bytes_as(value, o_type, encoding='utf-8', prefix=None, suffix=None, extension=None):
    p = '' if prefix is None else prefix
    s = '' if suffix is None else suffix
    if o_type == Types.STRING:
        return (p + value + s).encode(encoding), 'text/plain'
    elif o_type == Types.DICT:
        return (p + json.dumps(value, cls=utils.JSONEncoder) + s).encode(encoding), 'text/plain'
    elif o_type == Types.PILLOW_IMAGE:
        objct = BytesIO()
        fmt = value.format if hasattr(value, 'format') and value.format is not None else 'PNG'
        value.save(objct, fmt)
        objct.seek(0)
        return objct.getvalue(), __extension_types.get(extension, __extension_types.get(fmt.lower(), 'text/plain'))
    else:
        raise TypeError('Unhandled type')


@_resolve_location(require_key=True)
def write(value, *location, bucket=None, key=None, uri=None, newline='\n', acl=None, content_type=None,
          content_encoding=None, content_language=None, content_length=None, metadata=None, sse=None,
          storage_class=None,  tags=None, s3_resource=None, **params):
    """
    Write an object to the bucket/key pair (or uri), converting the python
    object to an appropriate format to write to file.

    :param value: Object to write to S3
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param acl: The canned ACL to apply to the object
    :param newline: Character(s) to use as a newline for list objects
    :param content_type: Content type to apply to the file, if not present a suggested type will be applied
    :param content_encoding: Specifies what content encodings have been applied to the object and thus what decoding
        mechanisms must be applied to obtain the media-type referenced by the Content-Type header field.
    :param content_language: The language the content is in.
    :param content_length: Size of the body in bytes.
    :param metadata: A map of metadata to store with the object in S3.
    :param sse: The server-side encryption algorithm used when storing this object in Amazon S3.
    :param storage_class: The S3 storage class to store the object in.
    :param tags: The tag-set for the object. Can be either a dict or url encoded key/value string.
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The URI of the object written to S3
    """
    extension = key.split('.')[-1]
    # JSON
    if isinstance(value, Mapping):
        return write_as(value, Types.DICT, bucket=bucket, key=key, uri=uri, acl=acl, newline=newline,
                        content_type=content_type, content_encoding=content_encoding,
                        content_language=content_language, content_length=content_length, metadata=metadata, sse=sse,
                        storage_class=storage_class, tags=tags, s3_resource=s3_resource)
    # Text
    elif isinstance(value, str):
        return write_as(value, Types.STRING, bucket=bucket, key=key, uri=uri, acl=acl, newline=newline,
                        content_type=content_type, content_encoding=content_encoding,
                        content_language=content_language, content_length=content_length, metadata=metadata, sse=sse,
                        storage_class=storage_class, tags=tags, s3_resource=s3_resource)

    # List
    # TODO: Handle iterables
    elif isinstance(value, list):
        if content_type is None:
            content_type = __extension_types.get(extension, 'text/plain')
        buff = StringIO()
        for row in value:
            if isinstance(row, Mapping):
                buff.write(json.dumps(row, cls=utils.JSONEncoder) + newline)
            else:
                buff.write(str(row) + newline)
        return __write(buff.getvalue(), bucket=bucket, key=key, uri=uri, acl=acl, content_type=content_type,
                       content_encoding=content_encoding, content_language=content_language,
                       content_length=content_length, metadata=metadata, sse=sse, storage_class=storage_class,
                       tags=tags, s3_resource=s3_resource)

    elif isinstance(value, StringIO):
        return __write(value.getvalue(), bucket=bucket, key=key, uri=uri, acl=acl, content_type=content_type,
                       content_encoding=content_encoding, content_language=content_language,
                       content_length=content_length, metadata=metadata, sse=sse, storage_class=storage_class,
                       tags=tags, s3_resource=s3_resource)
    elif isinstance(value, BytesIO):
        value.seek(0)
        return __write(value.getvalue(), bucket=bucket, key=key, uri=uri, acl=acl, content_type=content_type,
                       content_encoding=content_encoding, content_language=content_language,
                       content_length=content_length, metadata=metadata, sse=sse, storage_class=storage_class,
                       tags=tags, s3_resource=s3_resource)
    elif value is None:
        return __write('', bucket=bucket, key=key, uri=uri, acl=acl, s3_resource=s3_resource, content_type=content_type,
                       content_encoding=content_encoding, content_language=content_language,
                       content_length=content_length, metadata=metadata, sse=sse, storage_class=storage_class,
                       tags=tags)

    # primarily for Pillow images
    elif hasattr(value, 'save') and callable(getattr(value, 'save', None)):
        try:
            # Retrieve the content type based on whatever data we have available
            if content_type is None:
                content_type = __extension_types.get(extension,
                                                     __extension_types.get(params.get('format', '').lower(), 'image'))

            # Confirm that a format has been provided, else try to infer it or default to PNG
            if params.get('format') is None:
                if hasattr(value, 'format') and value.format is not None:
                    params['format'] = value.format
                elif content_type in __content_type_to_pillow_format.keys():
                    params['format'] = __content_type_to_pillow_format[content_type]
                else:
                    params['format'] = 'PNG'

            objct = BytesIO()
            value.save(objct, **params)
            objct.seek(0)
            return __write(objct, bucket=bucket, key=key, uri=uri, acl=acl, content_type=content_type,
                           content_encoding=content_encoding, content_language=content_language,
                           content_length=content_length,
                           metadata=metadata, sse=sse, storage_class=storage_class, tags=tags,
                           s3_resource=s3_resource)
        except Exception as e:
            pass

    # primarily for numpy arrays
    elif hasattr(value, 'tofile') and callable(getattr(value, 'tofile', None)):
        try:
            with TemporaryFile() as fp:
                value.tofile(fp, **params)
                fp.seek(0)
                return upload(fp, bucket=bucket, key=key, uri=uri, acl=acl, content_type=content_type,
                              content_encoding=content_encoding, content_language=content_language,
                              content_length=content_length, metadata=metadata, sse=sse, storage_class=storage_class,
                              tags=tags, s3_resource=s3_resource)
        except Exception as e:
            pass

    return __write(value, bucket=bucket, key=key, uri=uri, acl=acl, content_type=content_type,
                   content_encoding=content_encoding, content_language=content_language,
                   content_length=content_length, metadata=metadata, sse=sse, storage_class=storage_class,
                   tags=tags, s3_resource=s3_resource)


def _array_to_string(row, delimiter, indices=None):
    if indices is None:
        indices = range(len(row))
    line = ''
    for x in indices:
        line = str(row[x]) if x == 0 else line + delimiter + str(row[x])
    return line


@_resolve_location(require_key=True)
def write_delimited(rows, *location, bucket=None, key=None, uri=None, acl=None, newline='\n', delimiter=',',
                    columns=None, headers=None, content_type=None, content_encoding=None, content_language=None,
                    content_length=None, metadata=None, sse=None, storage_class=None,  tags=None,
                    s3_resource=None):
    """
    Write an object to the bucket/key pair (or uri), converting the python
    object to an appropriate format to write to file.

    :param rows: List of data to write, rows can be of type list, dict or str
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param acl: The canned ACL to apply to the object
    :param newline: Character(s) to use as a newline for list objects
    :param delimiter: Column delimiter to use, ',' by default
    :param columns: The columns to write out from the source rows, dict keys or list indexes
    :param headers: Headers to add to the output
    :param content_type: Content type to apply to the file, if not present a suggested type will be applied
    :param content_encoding: Specifies what content encodings have been applied to the object and thus what decoding
        mechanisms must be applied to obtain the media-type referenced by the Content-Type header field.
    :param content_language: The language the content is in.
    :param content_length: Size of the body in bytes.
    :param metadata: A map of metadata to store with the object in S3.
    :param sse: The server-side encryption algorithm used when storing this object in Amazon S3.
    :param storage_class: The S3 storage class to store the object in.
    :param tags: The tag-set for the object. Can be either a dict or url encoded key/value string.
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The URI of the object written to S3
    """
    return write_as(rows, Types.DELIMITED, bucket=bucket, key=key, uri=uri, acl=acl, newline=newline,
                    delimiter=delimiter, columns=columns, headers=headers, content_type=content_type,
                    content_encoding=content_encoding, content_language=content_language, content_length=content_length,
                    metadata=metadata, sse=sse, storage_class=storage_class, tags=tags,
                    s3_resource=s3_resource)


@_resolve_location(require_key=True)
def __append(content, *location, bucket=None, key=None, uri=None, s3_resource=None):
    # load the object and build the parameters that will be used to rewrite it
    objct = Object(bucket, key, s3_resource=s3_resource)
    values = {
        'content_encoding': objct.content_encoding,
        'content_language': objct.content_language,
        'content_type': objct.content_type,
        'metadata': objct.metadata,
        'sse': objct.server_side_encryption,
        'storage_class': objct.storage_class
    }
    params = larry.core.map_parameters(values, {
        'content_encoding': 'ContentEncoding',
        'content_language': 'ContentLanguage',
        'content_length': 'ContentLength',
        'content_type': 'ContentType',
        'metadata': 'Metadata',
        'sse': 'ServerSideEncryption',
        'storage_class': 'StorageClass',
    })
    tags = objct.tags
    if len(tags.keys()) > 0:
        params['Tagging'] = parse.urlencode(tags)

    # get the current ACL
    acl = objct.Acl()
    grants = acl.grants
    owner = acl.owner

    body = objct.get()['Body'].read() + content
    objct.put(Body=body, **params)
    objct.Acl().put(AccessControlPolicy={
        'Grants': grants,
        'Owner': owner
    })


@_resolve_location(require_key=True)
def append(value, *location, bucket=None, key=None, uri=None, incl_newline=True, newline='\n', encoding='utf-8',
           s3_resource=None):
    """
    Append content to the end of an s3 object. Assumes that the data should be treated as text in most cases.

    Note that this is not efficient as it requires a read/write for each call and isn't thread safe. It is only
    intended as a helper for simple operations such as capturing infrequent events and should not be used in a
    multi-threaded or multi-user environment.

    :param value: Data to write
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param incl_newline: Indicates if a newline character should be appended to the value
    :param newline: Newline character to append to the value
    :param encoding: Encoding to use when writing str to bytes
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    """
    # the append logic is designed around text based files so we'll convert int and float values to string first
    if isinstance(value, int) or isinstance(value, float):
        value = str(value)

    # write out the new string
    if isinstance(value, str):
        __append(__value_bytes_as(value,
                                  Types.STRING,
                                  encoding=encoding,
                                  suffix=newline if incl_newline else None)[0],
                 bucket=bucket, key=key, s3_resource=s3_resource)

    # write out the new json
    elif isinstance(value, Mapping):
        __append(__value_bytes_as(value,
                                  Types.DICT,
                                  encoding=encoding,
                                  suffix=newline if incl_newline else None)[0],
                 bucket=bucket, key=key, s3_resource=s3_resource)

    # iterate through a list of values using the same write approach
    elif hasattr(value, '__iter__'):
        buff = BytesIO()
        for v in value:
            if isinstance(v, int) or isinstance(v, float):
                v = str(v)
            if isinstance(v, str):
                buff.write(__value_bytes_as(v, Types.STRING, encoding=encoding,
                                            suffix=newline if incl_newline else None)[0])
            elif isinstance(v, Mapping):
                buff.write(__value_bytes_as(v, Types.DICT, encoding=encoding,
                                            suffix=newline if incl_newline else None)[0])
            else:
                buff.write(v)
        buff.seek(0)
        __append(buff.getvalue(), bucket=bucket, key=key, s3_resource=s3_resource)

    # hope that the value is in a byte format that can be appended to the existing content
    else:
        __append(value, bucket=bucket, key=key, s3_resource=s3_resource)


@_resolve_location(require_key=True)
def append_as(value, o_type, *location, bucket=None, key=None, uri=None, incl_newline=True, newline='\n',
              delimiter=',', columns=None, encoding='utf-8', s3_resource=None):
    """
    Append content to the end of an s3 object using the specified type to convert it prior to writing.

    Note that this is not efficient as it requires a read/write for each call and isn't thread safe. It is only
    intended as a helper for simple operations such as capturing infrequent events and should not be used in a
    multi-threaded or multi-user environment.

    :param value: Object to write to S3
    :param o_type: A value defined in larry.types to write the data using
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param incl_newline: Boolean to indicate if a newline should be added after the content
    :param newline: Character(s) to use as a newline for list objects
    :param delimiter: Column delimiter to use, ',' by default
    :param columns: The columns to write out from the source rows, dict keys or list indexes
    :param encoding: default encoding to apply to text when converting it to bytes
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The URI of the object written to S3
    """
    content = None
    extension = key.split('.')[-1]

    # if we're writing as a dict or string
    # iterate through if it's iterable, else write out the value
    if o_type in (Types.DICT, Types.STRING):
        if hasattr(value, '__iter__') and not isinstance(value, str) and not isinstance(value, Mapping):
            buff = BytesIO()
            for v in value:
                buff.write(__value_bytes_as(v, o_type, encoding=encoding, suffix=newline if incl_newline else None)[0])
            buff.seek(0)
            content = buff.getvalue()
        else:
            content = __value_bytes_as(value, o_type, encoding=encoding, suffix=newline if incl_newline else None)[0]

    # write out delimited values
    elif o_type == Types.DELIMITED:
        # if it's a dict or string just write them out
        if isinstance(value, Mapping):
            content = __value_mapping_to_delimited_bytes(value, columns=columns, newline=newline,
                                                         delimiter=delimiter, encoding=encoding)
        elif isinstance(value, str):
            content = __value_bytes_as(value, Types.STRING, encoding=encoding, extension=extension)[0]

        # if it's a list, handle the cases where it may be a list of rows instead of just one row
        elif hasattr(value, '__iter__'):
            value = list(value)

            # if it contains dicts, write those out as rows
            if isinstance(value[0], Mapping):
                buff = BytesIO()
                for v in value:
                    buff.write(__value_mapping_to_delimited_bytes(v, columns=columns, newline=newline,
                                                                  delimiter=delimiter, encoding=encoding)[0])
                buff.seek(0)
                content = buff.getvalue()

            # if it contains strings then this is just a single row of values
            elif isinstance(value[0], str):
                content = __value_bytes_as(_array_to_string(value, delimiter, columns), Types.STRING,
                                           encoding=encoding, suffix=newline)[0]

            # if it contains inner lists, then it was a 2d array of values and we'll want to write those out
            elif hasattr(value[0], '__iter__'):
                buff = BytesIO()
                for v in value:
                    buff.write(__value_bytes_as(_array_to_string(v, delimiter, columns), Types.STRING,
                                                encoding=encoding, suffix=newline)[0])
                buff.seek(0)
                content=buff.getvalue()

            # else assume it was non-string values that can be written out
            else:
                content = __value_bytes_as(_array_to_string(value, delimiter, columns), Types.STRING,
                                           encoding=encoding, suffix=newline)[0]

        # else it's hopefully some type of value that can be appended to bytes (ignoring newline)
        else:
            content = value
    else:
        raise TypeError('Unhandled type')
    __append(content, bucket=bucket, key=key, s3_resource=s3_resource)


def __value_mapping_to_delimited_bytes(value, columns=None, newline='\n', delimiter=',', encoding='utf-8'):
    keys = columns if columns else value.keys()
    buff = StringIO()
    for i, k in enumerate(keys):
        if i > 0:
            buff.write(delimiter)
        buff.write(str(value._get(k, '')))
    return __value_bytes_as(buff.getvalue(), Types.STRING, encoding=encoding, suffix=newline)


@resolve_client(_get_resource, 's3_resource')
def rename(old_bucket=None, old_key=None, old_uri=None, new_bucket=None, new_key=None, new_uri=None,
           s3_resource=None):
    """
    Renames an object in S3.

    :param old_bucket: Source bucket
    :param old_key: Source key
    :param old_uri: An s3:// path containing the bucket and key of the source object
    :param new_bucket: Target bucket
    :param new_key: Target key
    :param new_uri: An s3:// path containing the bucket and key of the source object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: None
    """
    # TODO: Add support for passing location without parameter names
    if old_uri:
        (old_bucket, old_key) = decompose_uri(old_uri)
    if new_uri:
        (new_bucket, new_key) = decompose_uri(new_uri)
    s3_resource = s3_resource if s3_resource else __resource
    copy_source = {
        'Bucket': old_bucket,
        'Key': old_key
    }
    s3_resource.meta.client.copy(copy_source, new_bucket, new_key)
    s3_resource.meta.client.delete_object(Bucket=old_bucket, Key=old_key)


@resolve_client(_get_resource, 's3_resource')
def copy(src_bucket=None, src_key=None, src_uri=None, new_bucket=None, new_key=None, new_uri=None,
         s3_resource=None):
    """
    Copies an object in S3.

    :param src_bucket: Source bucket
    :param src_key: Source key
    :param src_uri: An s3:// path containing the bucket and key of the source object
    :param new_bucket: Target bucket
    :param new_key: Target key
    :param new_uri: An s3:// path containing the bucket and key of the source object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: None
    """
    # TODO: Add support for passing location without parameter names
    if src_uri:
        (src_bucket, src_key) = decompose_uri(src_uri)
    if new_uri:
        (new_bucket, new_key) = decompose_uri(new_uri)
    s3_resource.meta.client.copy({'Bucket': src_bucket, 'Key': src_key}, new_bucket, new_key)


@_resolve_location(require_key=True)
def exists(*location, bucket=None, key=None, uri=None, s3_resource=None):
    """
    Checks to see if an object with the given bucket/key (or uri) exists.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for the object
    :param key: The key of the object
    :param uri: An s3:// path containing the bucket and key of the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: True if the key exists, if not, False
    """
    return Object(bucket=bucket, key=key, s3_resource=s3_resource).exists


def decompose_uri(uri):
    """
    Decompose an S3 URI into a bucket and key

    :param uri: S3 URI
    :return: Tuple containing a bucket and key
    """
    if isinstance(uri, str):
        m = URI_REGEX.match(uri)
        if m:
            return m.groups()
    return None, None


def get_bucket_name(uri):
    """
    Retrieve the bucket portion from an S3 URI

    :param uri: S3 URI
    :return: Bucket name
    """
    return decompose_uri(uri)[0]


def get_object_key(uri):
    """
    Retrieves the key portion of an S3 URI

    :param uri: S3 URI
    :return: Key value
    """
    return decompose_uri(uri)[1]


def compose_uri(bucket, key=None):
    """
    Compose a bucket and key into an S3 URI

    :param bucket: Bucket name
    :param key: Object key
    :return: S3 URI string
    """
    if key:
        return "s3://{}/{}".format(bucket, key)
    else:
        return "s3://{}/".format(bucket)


@resolve_client(_get_resource, 's3_resource')
@_resolve_location(key_arg='prefix')
def list_objects(*location, bucket=None, prefix=None, uri=None, include_empty_objects=False, s3_resource=None):
    """
    Returns a iterable of the keys in the bucket that begin with the provided prefix.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket to query
    :param prefix: The key prefix to use in searching the bucket
    :param uri: An s3:// path containing the bucket and prefix
    :param include_empty_objects: True if you want to include keys associated with objects of size=0
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: A generator of object keys
    """
    paginator = s3_resource.meta.client.get_paginator('list_objects')
    operation_parameters = {'Bucket': bucket}
    if prefix:
        operation_parameters['Prefix'] = prefix
    page_iterator = paginator.paginate(**operation_parameters)
    for page in page_iterator:
        for objct in page._get('Contents', []):
            if objct['Size'] > 0 or include_empty_objects:
                yield objct['Key']


def _find_largest_common_prefix(values):
    """
    Searches through a list of values to find the longest possible common prefix amongst them. Useful for optimizing
    more costly searches. Supports lists of strings or tuples. If tuples are used, the first value is assumed to be
    the value to search on.

    :param values: List of values (strings or tuples containing a string in the first position)
    :return: String prefix common to all values
    """
    if isinstance(values[0], tuple):
        prefix, *_ = values[0]
    else:
        prefix = values[0]

    for value in values:
        key = value[0] if isinstance(value, tuple) else value
        while key[:len(prefix)] != prefix and len(prefix) > 0:
            prefix = prefix[:-1]
    return prefix


def find_keys_not_present(bucket, keys=None, uris=None, s3_resource=None):
    """
    Searches an S3 bucket for a list of keys and returns any that cannot be found.

    :param bucket: The S3 bucket to search
    :param keys: A list of keys to search for (strings or tuples containing a string in the first position)
    :param uris: A list of S3 URIs to search for (strings or tuples containing a string in the first position)
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: A list of keys that were not found (strings or tuples based on the input values)
    """

    # If URIs are passed, convert to a list of keys to use for the search
    if uris:
        keys = []
        for value in uris:
            if isinstance(value, tuple):
                uri, *z = value
                b, key = decompose_uri(uri)
                keys.append(tuple([key]) + tuple(z))
            else:
                b, key = decompose_uri(value)
                keys.append(key)

    # Find the longest common prefix to use as the search term
    prefix = _find_largest_common_prefix(keys)

    # Get a list of all keys in the bucket that match the prefix
    bucket_obj = Bucket(bucket=bucket, s3_resource=s3_resource)
    all_keys = []
    for objct in bucket_obj.objects.filter(Prefix=prefix):
        all_keys.append(objct.key)

    # Search for any keys that can't be found
    not_found = []
    for value in keys:
        key = value[0] if isinstance(value, tuple) else value
        if key not in all_keys:
            not_found.append(value)
    return not_found


@_resolve_location(require_key=True)
def fetch(url, *location, bucket=None, key=None, uri=None, s3_resource=None, acl=None, **kwargs):
    """
    Retrieves the data referenced by a URL to an S3 location.

    :param url: URL to retrieve
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for the object
    :param key: The key of the object
    :param uri: An s3:// path containing the bucket and key of the object
    :param acl: An S3 policy to apply the S3 location
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The URI of the object written to S3
    """
    req = request.Request(url, **kwargs)
    with request.urlopen(req) as response:
        return __write(response.read(), bucket=bucket, key=key, s3_resource=s3_resource, acl=acl)


@_resolve_location(require_key=True)
def download(file, *location, bucket=None, key=None, uri=None, use_threads=True, s3_resource=None):
    """
    Downloads the an S3 object to a directory on the local file system.

    :param file: The file, file-like object, or directory to download the object to
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param use_threads: Enables the use_threads transfer config
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: Path of the local file
    """
    config = TransferConfig(use_threads=use_threads)
    objct = Object(bucket=bucket, key=key, s3_resource=s3_resource)
    if isinstance(file, str):
        if os.path.isdir(file):
            file = os.path.join(file, key.split('/')[-1])
        objct.download_file(file, Config=config)
    else:
        objct.download_fileobj(file, Config=config)

    return file


@_resolve_location(require_key=True)
def download_to_temp(*location, bucket=None, key=None, uri=None, s3_resource=None):
    """
    Downloads the an S3 object to a temp directory on the local file system.

    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: A file pointer to the temp file
    """
    fp = TemporaryFile()
    download(fp, bucket=bucket, key=key, uri=uri, s3_resource=s3_resource)
    fp.seek(0)
    return fp


@_resolve_location(require_key=True)
def upload(file, *location, bucket=None, key=None, uri=None, acl=None, content_type=None, content_encoding=None,
           content_language=None, content_length=None, metadata=None, sse=None, storage_class=None,
           tags=None, s3_resource=None):
    """
    Uploads a local file to S3

    :param file: The file, file-like object, or directory to upload
    :param location: Positional values for bucket, key, and/or uri
    :param bucket: The S3 bucket for object to retrieve
    :param key: The key of the object to be retrieved from the bucket
    :param uri: An s3:// path containing the bucket and key of the object
    :param acl: The canned ACL to apply to the object
    :param content_type: Content type to apply to the file
    :param content_encoding: Specifies what content encodings have been applied to the object and thus what decoding
        mechanisms must be applied to obtain the media-type referenced by the Content-Type header field.
    :param content_language: The language the content is in.
    :param content_length: Size of the body in bytes.
    :param metadata: A map of metadata to store with the object in S3.
    :param sse: The server-side encryption algorithm used when storing this object in Amazon S3.
    :param storage_class: The S3 storage class to store the object in.
    :param tags: The tag-set for the object. Can be either a dict or url encoded key/value string.
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The uri of the file in S3
    """
    extra = larry.core.map_parameters(locals(), {
        'acl': 'ACL',
        'content_encoding': 'ContentEncoding',
        'content_language': 'ContentLanguage',
        'content_length': 'ContentLength',
        'content_type': 'ContentType',
        'metadata': 'Metadata',
        'sse': 'ServerSideEncryption',
        'storage_class': 'StorageClass',
    })
    if tags:
        extra['Tagging'] = parse.urlencode(tags) if isinstance(tags, Mapping) else tags
    params = {} if len(extra.keys()) == 0 else {'ExtraArgs': extra}
    objct = Object(bucket=bucket, key=key, s3_resource=s3_resource)
    if isinstance(file, str):
        objct.upload_file(file, **params)
    else:
        objct.upload_fileobj(file, **params)
    return compose_uri(bucket, key)


def write_temp(value, prefix, acl=None, s3_resource=None, bucket_identifier=None, region=None,
               bucket=None):
    """
    Write an object to a temp bucket with a unique UUID.

    :param value: Object to write to S3
    :param prefix: Prefix to attach ahead of the UUID as the key
    :param acl: The canned ACL to apply to the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :param bucket_identifier: The identifier to attach to the temp bucket that will be used for writing to s3, typically
        the account id (from STS) for the account being used
    :param region: The s3 region to store the data in
    :param bucket: The bucket to use instead of creating/using a temp bucket
    :return: The URI of the object written to S3
    """
    if bucket is None:
        bucket = get_temp_bucket(region=region, bucket_identifier=bucket_identifier, s3_resource=s3_resource)
    key = prefix + str(uuid.uuid4())
    return write(value, bucket=bucket, key=key, acl=acl, s3_resource=s3_resource)


@_resolve_location(require_key=True)
def make_public(*location, bucket=None, key=None, uri=None, s3_resource=None):
    """
    Makes the object defined by the bucket/key pair (or uri) public.

    :param bucket: The S3 bucket for object
    :param key: The key of the object
    :param uri: An s3:// path containing the bucket and key of the object
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :return: The URL of the object
    """
    return Object(bucket=bucket, key=key, s3_resource=s3_resource).make_public()


def _object_url(bucket, key):
    return 'https://{}.s3.amazonaws.com/{}'.format(bucket, parse.quote(key))


def _bucket_url(bucket):
    return 'https://{}.s3.amazonaws.com'.format(bucket)


@_resolve_location()
def get_public_url(*location, bucket=None, key=None, uri=None):
    """
    Returns the public URL of an S3 object (assuming it's public).

    :param bucket: The S3 bucket for object
    :param key: The key of the object
    :param uri: An s3:// path containing the bucket and key of the object
    :return: The URL of the object
    """
    if key:
        _object_url(bucket, key)
    else:
        _bucket_url(bucket)


def create_bucket(bucket, acl=ACL_PRIVATE, region=None, s3_resource=None):
    """
    Create a bucket in S3 and waits until it has been created.

    :param bucket: The name of the bucket
    :param acl: The canned ACL to apply to the object
    :param region: The region to location the S3 bucket, defaults to the region of the current session
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    """
    if region is None:
        region = __session.region_name
    bucket_obj = Bucket(bucket=bucket, s3_resource=s3_resource)
    bucket_obj.create(ACL=acl, CreateBucketConfiguration={'LocationConstraint': region})
    bucket_obj.wait_until_exists()


def delete_bucket(bucket, s3_resource=None):
    """
    Delete an S3 bucket.

    :param bucket: The name of the bucket
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    """
    bucket_obj = Bucket(bucket=bucket, s3_resource=s3_resource)
    bucket_obj.delete()
    bucket_obj.wait_until_not_exists()


def get_temp_bucket(region=None, s3_resource=None, bucket_identifier=None):
    """
    Create a bucket that will be used as temp storage for larry commands.
    The bucket will be created in the region associated with the current session
    using a name based on the current session account id and region.

    :param region: Region to locate the temp bucket
    :param s3_resource: Boto3 resource to use if you don't wish to use the default resource
    :param bucket_identifier: The bucket identifier to use as a unique identifier for the bucket, defaults to the
        account id associated with the session
    :return: The name of the created bucket
    """
    if region is None:
        region = __session.region_name
    if bucket_identifier is None:
        bucket_identifier = sts.account_id()
    bucket = '{}-larry-{}'.format(bucket_identifier, region)
    create_bucket(bucket, region=region, s3_resource=s3_resource)
    return bucket


# TODO: add filter parameter
# TODO: rationalize the list params
def download_to_zip(file, bucket, prefix=None, prefixes=None):
    if prefix:
        prefixes = [prefix]
    with ZipFile(file, 'w') as zfile:
        for prefix in prefixes:
            for key in list_objects(bucket, prefix):
                zfile.writestr(parse.quote(key), data=read(bucket, key))


def file_name_portion(uri):
    file = decompose_uri(uri)[1].split('/')[-1]
    return file[:file.rfind('.')]


class error_codes(Enum):
    AccessDenied = 'AccessDenied'
    AccountProblem = 'AccountProblem'
    AllAccessDisabled = 'AllAccessDisabled'
    AmbiguousGrantByEmailAddress = 'AmbiguousGrantByEmailAddress'
    AuthorizationHeaderMalformed = 'AuthorizationHeaderMalformed'
    BadDigest = 'BadDigest'
    BucketAlreadyExists = 'BucketAlreadyExists'
    BucketAlreadyOwnedByYou = 'BucketAlreadyOwnedByYou'
    BucketNotEmpty = 'BucketNotEmpty'
    CredentialsNotSupported = 'CredentialsNotSupported'
    CrossLocationLoggingProhibited = 'CrossLocationLoggingProhibited'
    EntityTooSmall = 'EntityTooSmall'
    EntityTooLarge = 'EntityTooLarge'
    ExpiredToken = 'ExpiredToken'
    IllegalLocationConstraintException = 'IllegalLocationConstraintException'
    IllegalVersioningConfigurationException = 'IllegalVersioningConfigurationException'
    IncompleteBody = 'IncompleteBody'
    IncorrectNumberOfFilesInPostRequest = 'IncorrectNumberOfFilesInPostRequest'
    InlineDataTooLarge = 'InlineDataTooLarge'
    InternalError = 'InternalError'
    InvalidAccessKeyId = 'InvalidAccessKeyId'
    InvalidAddressingHeader = 'InvalidAddressingHeader'
    InvalidArgument = 'InvalidArgument'
    InvalidBucketName = 'InvalidBucketName'
    InvalidBucketState = 'InvalidBucketState'
    InvalidDigest = 'InvalidDigest'
    InvalidEncryptionAlgorithmError = 'InvalidEncryptionAlgorithmError'
    InvalidLocationConstraint = 'InvalidLocationConstraint'
    InvalidObjectState = 'InvalidObjectState'
    InvalidPart = 'InvalidPart'
    InvalidPartOrder = 'InvalidPartOrder'
    InvalidPayer = 'InvalidPayer'
    InvalidPolicyDocument = 'InvalidPolicyDocument'
    InvalidRange = 'InvalidRange'
    InvalidRequest = 'InvalidRequest'
    InvalidSecurity = 'InvalidSecurity'
    InvalidSOAPRequest = 'InvalidSOAPRequest'
    InvalidStorageClass = 'InvalidStorageClass'
    InvalidTargetBucketForLogging = 'InvalidTargetBucketForLogging'
    InvalidToken = 'InvalidToken'
    InvalidURI = 'InvalidURI'
    KeyTooLongError = 'KeyTooLongError'
    MalformedACLError = 'MalformedACLError'
    MalformedPOSTRequest = 'MalformedPOSTRequest'
    MalformedXML = 'MalformedXML'
    MaxMessageLengthExceeded = 'MaxMessageLengthExceeded'
    MaxPostPreDataLengthExceededError = 'MaxPostPreDataLengthExceededError'
    MetadataTooLarge = 'MetadataTooLarge'
    MethodNotAllowed = 'MethodNotAllowed'
    MissingAttachment = 'MissingAttachment'
    MissingContentLength = 'MissingContentLength'
    MissingRequestBodyError = 'MissingRequestBodyError'
    MissingSecurityElement = 'MissingSecurityElement'
    MissingSecurityHeader = 'MissingSecurityHeader'
    NoLoggingStatusForKey = 'NoLoggingStatusForKey'
    NoSuchBucket = 'NoSuchBucket'
    NoSuchBucketPolicy = 'NoSuchBucketPolicy'
    NoSuchKey = 'NoSuchKey'
    NoSuchLifecycleConfiguration = 'NoSuchLifecycleConfiguration'
    NoSuchUpload = 'NoSuchUpload'
    NoSuchVersion = 'NoSuchVersion'
    NotImplemented = 'NotImplemented'
    NotSignedUp = 'NotSignedUp'
    OperationAborted = 'OperationAborted'
    PermanentRedirect = 'PermanentRedirect'
    PreconditionFailed = 'PreconditionFailed'
    Redirect = 'Redirect'
    RestoreAlreadyInProgress = 'RestoreAlreadyInProgress'
    RequestIsNotMultiPartContent = 'RequestIsNotMultiPartContent'
    RequestTimeout = 'RequestTimeout'
    RequestTimeTooSkewed = 'RequestTimeTooSkewed'
    RequestTorrentOfBucketError = 'RequestTorrentOfBucketError'
    ServerSideEncryptionConfigurationNotFoundError = 'ServerSideEncryptionConfigurationNotFoundError'
    ServiceUnavailable = 'ServiceUnavailable'
    SignatureDoesNotMatch = 'SignatureDoesNotMatch'
    SlowDown = 'SlowDown'
    TemporaryRedirect = 'TemporaryRedirect'
    TokenRefreshRequired = 'TokenRefreshRequired'
    TooManyBuckets = 'TooManyBuckets'
    UnexpectedContent = 'UnexpectedContent'
    UnresolvableGrantByEmailAddress = 'UnresolvableGrantByEmailAddress'
    UserKeyMustBeSpecified = 'UserKeyMustBeSpecified'

