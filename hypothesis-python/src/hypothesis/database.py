# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Most of this work is copyright (C) 2013-2020 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.
#
# END HEADER

import abc
import binascii
import os
import warnings
from hashlib import sha384
from typing import Iterable

from hypothesis.configuration import mkdir_p, storage_directory
from hypothesis.errors import HypothesisException, HypothesisWarning
from hypothesis.utils.conventions import not_set

__all__ = [
    "DirectoryBasedExampleDatabase",
    "ExampleDatabase",
    "InMemoryExampleDatabase",
]


def _usable_dir(path):
    """
    Returns True iff the desired path can be used as database path because
    either the directory exists and can be used, or its root directory can
    be used and we can make the directory as needed.
    """
    while not os.path.exists(path):
        # Loop terminates because the root dir ('/' on unix) always exists.
        path = os.path.dirname(path)
    return os.path.isdir(path) and os.access(path, os.R_OK | os.W_OK | os.X_OK)


def _db_for_path(path=None):
    if path is not_set:
        if os.getenv("HYPOTHESIS_DATABASE_FILE") is not None:  # pragma: no cover
            raise HypothesisException(
                "The $HYPOTHESIS_DATABASE_FILE environment variable no longer has any "
                "effect.  Configure your database location via a settings profile instead.\n"
                "https://hypothesis.readthedocs.io/en/latest/settings.html#settings-profiles"
            )

        path = storage_directory("examples")
        if not _usable_dir(path):  # pragma: no cover
            warnings.warn(
                HypothesisWarning(
                    "The database setting is not configured, and the default "
                    "location is unusable - falling back to an in-memory "
                    "database for this session.  path=%r" % (path,)
                )
            )
            return InMemoryExampleDatabase()
    if path in (None, ":memory:"):
        return InMemoryExampleDatabase()
    return DirectoryBasedExampleDatabase(str(path))


class _EDMeta(abc.ABCMeta):
    def __call__(self, *args, **kwargs):
        if self is ExampleDatabase:
            return _db_for_path(*args, **kwargs)
        return super().__call__(*args, **kwargs)


class ExampleDatabase(metaclass=_EDMeta):
    """An abstract base class for storing examples in Hypothesis' internal format.

    An ExampleDatabase maps each ``bytes`` key to many distinct ``bytes``
    values, like a ``Mapping[bytes, AbstractSet[bytes]]``.
    """

    @abc.abstractmethod
    def save(self, key: bytes, value: bytes) -> None:
        """Save ``value`` under ``key``.

        If this value is already present for this key, silently do nothing.
        """
        raise NotImplementedError("%s.save" % (type(self).__name__))

    @abc.abstractmethod
    def fetch(self, key: bytes) -> Iterable[bytes]:
        """Return an iterable over all values matching this key."""
        raise NotImplementedError("%s.fetch" % (type(self).__name__))

    @abc.abstractmethod
    def delete(self, key: bytes, value: bytes) -> None:
        """Remove this value from this key.

        If this value is not present, silently do nothing.
        """
        raise NotImplementedError("%s.delete" % (type(self).__name__))

    def move(self, src: bytes, dest: bytes, value: bytes) -> None:
        """Move ``value`` from key ``src`` to key ``dest``. Equivalent to
        ``delete(src, value)`` followed by ``save(src, value)``, but may
        have a more efficient implementation.

        Note that ``value`` will be inserted at ``dest`` regardless of whether
        it is currently present at ``src``.
        """
        if src == dest:
            self.save(src, value)
            return
        self.delete(src, value)
        self.save(dest, value)


class InMemoryExampleDatabase(ExampleDatabase):
    """A non-persistent example database, implemented in terms of a dict of sets.

    This can be useful if you call a test function several times in a single
    session, or for testing other database implementations, but because it
    does not persist between runs we do not recommend it for general use.
    """

    def __init__(self):
        self.data = {}

    def __repr__(self) -> str:
        return "InMemoryExampleDatabase(%r)" % (self.data,)

    def fetch(self, key: bytes) -> Iterable[bytes]:
        yield from self.data.get(key, ())

    def save(self, key: bytes, value: bytes) -> None:
        self.data.setdefault(key, set()).add(bytes(value))

    def delete(self, key: bytes, value: bytes) -> None:
        self.data.get(key, set()).discard(bytes(value))


def _hash(key):
    return sha384(key).hexdigest()[:16]


class DirectoryBasedExampleDatabase(ExampleDatabase):
    """Use a directory to store Hypothesis examples as files.

    This is the default database for Hypothesis; see above for details.

    .. i.e. see the documentation in database.rst
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.keypaths = {}  # type: dict

    def __repr__(self) -> str:
        return "DirectoryBasedExampleDatabase(%r)" % (self.path,)

    def _key_path(self, key):
        try:
            return self.keypaths[key]
        except KeyError:
            pass
        directory = os.path.join(self.path, _hash(key))
        self.keypaths[key] = directory
        return directory

    def _value_path(self, key, value):
        return os.path.join(self._key_path(key), _hash(value))

    def fetch(self, key: bytes) -> Iterable[bytes]:
        kp = self._key_path(key)
        if not os.path.exists(kp):
            return
        for path in os.listdir(kp):
            try:
                with open(os.path.join(kp, path), "rb") as i:
                    yield i.read()
            except OSError:
                pass

    def save(self, key: bytes, value: bytes) -> None:
        # Note: we attempt to create the dir in question now. We
        # already checked for permissions, but there can still be other issues,
        # e.g. the disk is full
        mkdir_p(self._key_path(key))
        path = self._value_path(key, value)
        if not os.path.exists(path):
            suffix = binascii.hexlify(os.urandom(16)).decode("ascii")
            tmpname = path + "." + suffix
            with open(tmpname, "wb") as o:
                o.write(value)
            try:
                os.rename(tmpname, path)
            except OSError:  # pragma: no cover
                os.unlink(tmpname)
            assert not os.path.exists(tmpname)

    def move(self, src: bytes, dest: bytes, value: bytes) -> None:
        if src == dest:
            self.save(src, value)
            return
        try:
            os.renames(self._value_path(src, value), self._value_path(dest, value))
        except OSError:
            self.delete(src, value)
            self.save(dest, value)

    def delete(self, key: bytes, value: bytes) -> None:
        try:
            os.unlink(self._value_path(key, value))
        except OSError:
            pass
