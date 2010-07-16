#! /usr/bin/env python
############################################################################
# Copyright (C) 2002  Paul Rodger <paul@paulrodger.com>,
#           (C) 2006  Peter Poeml <poeml@suse.de>,
#           (C) 2006-2008  Nikolaus Schulz <microschulz@web.de>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
############################################################################
"""
Archive and compress old mail in mbox, MH or maildir-format mailboxes.
Website: http://archivemail.sourceforge.net/
"""

# global administrivia 
__version__ = "archivemail v0.7.2"
__copyright__ = """\
Copyright (C) 2002  Paul Rodger <paul@paulrodger.com>
          (C) 2006  Peter Poeml <poeml@suse.de>,
          (C) 2006-2008  Nikolaus Schulz <microschulz@web.de>
This is free software; see the source for copying conditions. There is NO
warranty; not even for MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE."""

import sys

def check_python_version(): 
    """Abort if we are running on python < v2.3"""
    too_old_error = "This program requires python v2.3 or greater. " + \
      "Your version of python is:\n%s""" % sys.version
    try: 
        version = sys.version_info  # we might not even have this function! :)
        if (version[0] < 2) or (version[0] == 2 and version[1] < 3):
            print too_old_error
            sys.exit(1)
    except AttributeError:
        print too_old_error
        sys.exit(1)

# define & run this early
# (IMAP over SSL requires Python >= 2.3) 
check_python_version()  

import fcntl
import getopt
import gzip
import mailbox
import os
import pwd
import re
import rfc822
import shutil
import signal
import stat
import string
import tempfile
import time
import urlparse
import errno
import socket

# From_ mangling regex. 
from_re = re.compile(r'^From ', re.MULTILINE)
imapsize_re = re.compile(r'^(?P<msn>[0-9]+) \(RFC822\.SIZE (?P<size>[0-9]+)\)')

############## class definitions ###############

class ArchivemailException(Exception):
    pass
class UserError(ArchivemailException): 
    pass
class UnexpectedError(ArchivemailException): 
    pass
class LockUnavailable(ArchivemailException):
    pass

class Stats:
    """Class to collect and print statistics about mailbox archival"""
    __archived = 0
    __archived_size = 0
    __mailbox_name = None
    __archive_name = None
    __start_time = 0
    __total = 0
    __total_size = 0

    def __init__(self, mailbox_name, final_archive_name):
        """Constructor for a new set of statistics.

        Arguments: 
        mailbox_name -- filename/dirname of the original mailbox
        final_archive_name -- filename for the final 'mbox' archive, without
                              compression extension (eg .gz)

        """
        assert mailbox_name
        assert final_archive_name
        self.__start_time = time.time()
        self.__mailbox_name = mailbox_name
        self.__archive_name = final_archive_name + ".gz"

    def another_message(self, size):
        """Add one to the internal count of total messages processed 
        and record message size."""
        self.__total = self.__total + 1
        self.__total_size = self.__total_size + size

    def another_archived(self, size):
        """Add one to the internal count of messages archived
        and record message size."""
        self.__archived = self.__archived + 1
        self.__archived_size = self.__archived_size + size

    def display(self):
        """Print statistics about how many messages were archived"""
        end_time = time.time()
        time_seconds = end_time - self.__start_time
        action = "archived"
        if options.delete_old_mail:
            action = "deleted"
        if options.dry_run:
            action = "I would have " + action
        print "%s:\n    %s %d of %d message(s) (%s of %s) in %.1f seconds" % \
            (self.__mailbox_name, action, self.__archived, self.__total,
            nice_size_str(self.__archived_size), 
            nice_size_str(self.__total_size), time_seconds)
            

class StaleFiles:
    """Class to keep track of files to be deleted on abnormal exit"""
    dotlock_files      = []    # dotlock files for source mbox and final archive
    temp_mboxes        = []    # temporary retain and archive mboxes
    temp_dir           = None  # our tempfile directory container

    def clean(self):
        """Delete any temporary files or lockfiles that exist"""
        while self.dotlock_files:
            dotlock = self.dotlock_files.pop()
            vprint("removing stale dotlock file '%s'" % dotlock)
            try: 
                os.remove(dotlock)
            except (IOError, OSError): pass
        while self.temp_mboxes:
            mbox = self.temp_mboxes.pop()
            vprint("removing stale temporary mbox '%s'" % mbox)
            try: 
                os.remove(mbox)
            except (IOError, OSError): pass
        if self.temp_dir:
            vprint("removing stale tempfile directory '%s'" % self.temp_dir)
            try: 
                os.rmdir(self.temp_dir)
            except OSError, e:
                if e.errno == errno.ENOTEMPTY: # Probably a bug
                    user_warning("cannot remove temporary directory '%s', "
                            "directory not empty" % self.temp_dir)
            except IOError: pass
            else: self.temp_dir = None



class Options:
    """Class to store runtime options, including defaults"""
    archive_suffix       = "_archive"
    days_old_max         = 180
    date_old_max         = None
    delete_old_mail      = 0
    dry_run              = 0
    filter_append        = None
    include_flagged      = 0
    locking_attempts     = 5
    lockfile_extension   = ".lock"
    lock_sleep           = 1
    no_compress          = 0
    only_archive_read    = 0
    output_dir           = None
    pwfile               = None
    preserve_unread      = 0
    mangle_from          = 1
    quiet                = 0
    read_buffer_size     = 8192
    script_name          = os.path.basename(sys.argv[0])
    min_size             = None
    verbose              = 0
    debug_imap           = 0
    warn_duplicates      = 0
    copy_old_mail        = 0
    archive_all          = 0

    def parse_args(self, args, usage):
        """Set our runtime options from the command-line arguments.

        Arguments:
        args -- this is sys.argv[1:]
        usage -- a usage message to display on '--help' or bad arguments

        Returns the remaining command-line arguments that have not yet been
        parsed as a string.

        """
        try:
            opts, args = getopt.getopt(args, '?D:S:Vd:hno:F:P:qs:uv', 
                             ["date=", "days=", "delete", "dry-run", "help",
                             "include-flagged", "no-compress", "output-dir=",
                             "filter-append=", "pwfile=", "dont-mangle",
                             "preserve-unread", "quiet", "size=", "suffix=",
                             "verbose", "debug-imap=", "version", 
                             "warn-duplicate", "copy", "all"])
        except getopt.error, msg:
            user_error(msg)

        archive_by = None 

        for o, a in opts:
            if o == '--delete':
                if self.copy_old_mail: 
                    user_error("found conflicting options --copy and --delete")
                self.delete_old_mail = 1
            if o == '--include-flagged':
                self.include_flagged = 1
            if o == '--no-compress':
                self.no_compress = 1
            if o == '--warn-duplicate':
                self.warn_duplicates = 1
            if o in ('-D', '--date'):
                if archive_by: 
                    user_error("you cannot specify both -d and -D options")
                archive_by = "date"                        
                self.date_old_max = self.date_argument(a)
            if o in ('-d', '--days'):
                if archive_by: 
                    user_error("you cannot specify both -d and -D options")
                archive_by = "days"                        
                self.days_old_max = string.atoi(a)
            if o in ('-o', '--output-dir'):
                self.output_dir = os.path.expanduser(a)
            if o in ('-P', '--pwfile'):
                self.pwfile = os.path.expanduser(a)
            if o in ('-F', '--filter-append'):
                self.filter_append = a
            if o in ('-h', '-?', '--help'):
                print usage
                sys.exit(0)
            if o in ('-n', '--dry-run'):
                self.dry_run = 1
            if o in ('-q', '--quiet'):
                self.quiet = 1
            if o in ('-s', '--suffix'):
                self.archive_suffix = a
            if o in ('-S', '--size'):
                self.min_size = string.atoi(a)
            if o in ('-u', '--preserve-unread'):
                self.preserve_unread = 1
            if o == '--dont-mangle':
                self.mangle_from = 0
            if o in ('-v', '--verbose'):
                self.verbose = 1
            if o == '--debug-imap': 
                self.debug_imap = int(a)
            if o == '--copy':
                if self.delete_old_mail: 
                    user_error("found conflicting options --copy and --delete")
                self.copy_old_mail = 1
            if o == '--all': 
                self.archive_all = 1
            if o in ('-V', '--version'):
                print __version__ + "\n\n" + __copyright__
                sys.exit(0)
        return args

    def sanity_check(self):
        """Complain bitterly about our options now rather than later"""
        if self.output_dir:
            check_sane_destdir(self.output_dir)
        if self.days_old_max < 0:
            user_error("--days argument must be positive")
        if self.days_old_max >= 10000:
            user_error("--days argument must be less than 10000")
        if self.min_size is not None and self.min_size < 1:
            user_error("--size argument must be greater than zero")
        if self.quiet and self.verbose:
            user_error("you cannot use both the --quiet and --verbose options")
        if self.pwfile:
            if not os.path.isfile(self.pwfile):
                user_error("pwfile %s does not exist" % self.pwfile)

    def date_argument(self, string):
        """Converts a date argument string into seconds since the epoch"""
        date_formats = (
            "%Y-%m-%d",  # ISO format 
            "%d %b %Y" , # Internet format 
            "%d %B %Y" , # Internet format with full month names
        )
        time.accept2dyear = 0  # I'm not going to support 2-digit years
        for format in date_formats:
            try:
                date = time.strptime(string, format)
                seconds = time.mktime(date)
                return seconds
            except (ValueError, OverflowError):
                pass
        user_error("cannot parse the date argument '%s'\n"
            "The date should be in ISO format (eg '2002-04-23'),\n"
            "Internet format (eg '23 Apr 2002') or\n"
            "Internet format with full month names (eg '23 April 2002')" % 
            string)


class LockableMboxMixin:
    """Locking methods for mbox files."""

    def __init__(self, mbox_file, mbox_file_name):
        self.mbox_file = mbox_file
        self.mbox_file_name = mbox_file_name
        self._locked = False

    def lock(self):
        """Lock this mbox with both a dotlock and a posix lock."""
        assert not self._locked
        attempt = 1
        while True:
            try:
                self._posix_lock()
                self._dotlock_lock()
                break
            except LockUnavailable, e:
                self._posix_unlock()
                attempt += 1
                if (attempt > options.locking_attempts):
                    unexpected_error(str(e))
                vprint("%s - sleeping..." % e)
                time.sleep(options.lock_sleep)
            except:
                self._posix_unlock()
                raise
        self._locked = True

    def unlock(self):
        """Unlock this mbox."""
        assert self._locked
        self._dotlock_unlock()
        self._posix_unlock()
        self._locked = False

    def _posix_lock(self):
        """Set an exclusive posix lock on the 'mbox' mailbox"""
        vprint("trying to acquire posix lock on file '%s'" % self.mbox_file_name)
        try:
            fcntl.lockf(self.mbox_file, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except IOError, e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                raise LockUnavailable("posix lock for '%s' unavailable" % \
                    self.mbox_file_name)
            else:
                raise
        vprint("acquired posix lock on file '%s'" % self.mbox_file_name)

    def _posix_unlock(self):
        """Unset any posix lock on the 'mbox' mailbox"""
        vprint("dropping posix lock on file '%s'" % self.mbox_file_name)
        fcntl.lockf(self.mbox_file, fcntl.LOCK_UN)

    def _dotlock_lock(self):
        """Create a dotlock file for the 'mbox' mailbox"""
        hostname = socket.gethostname()
        pid = os.getpid()
        box_dir, prelock_prefix = os.path.split(self.mbox_file_name)
        prelock_suffix = ".%s.%s%s" % (hostname, pid, options.lockfile_extension)
        lock_name = self.mbox_file_name + options.lockfile_extension
        vprint("trying to create dotlock file '%s'" % lock_name)
        try:
            plfd, prelock_name = tempfile.mkstemp(prelock_suffix, prelock_prefix,
                dir=box_dir)
        except OSError, e:
            if e.errno == errno.EACCES:
                if not options.quiet:
                    user_warning("no write permissions: omitting dotlock for '%s'" % \
                        self.mbox_file_name)
                return
            raise
        try:
            try:
                os.link(prelock_name, lock_name)
                # We've got the lock.
            except OSError, e:
                if os.fstat(plfd)[stat.ST_NLINK] == 2:
                    # The Linux man page for open(2) claims that in this
                    # case we have actually succeeded to create the link,
                    # and this assumption seems to be folklore.
                    # So we've got the lock.
                    pass
                elif e.errno == errno.EEXIST:
                    raise LockUnavailable("Dotlock for '%s' unavailable" % self.mbox_file_name)
                else:
                    raise
            _stale.dotlock_files.append(lock_name)
        finally:
            os.close(plfd)
            os.unlink(prelock_name)
        vprint("acquired lockfile '%s'" % lock_name)

    def _dotlock_unlock(self):
        """Delete the dotlock file for the 'mbox' mailbox."""
        assert self.mbox_file_name
        lock_name = self.mbox_file_name + options.lockfile_extension
        vprint("removing lockfile '%s'" % lock_name)
        os.remove(lock_name)
        _stale.dotlock_files.remove(lock_name)

    def commit(self):
        """Sync the mbox file to disk."""
        self.mbox_file.flush()
        os.fsync(self.mbox_file.fileno())

    def close(self):
        """Close the mbox file"""
        vprint("closing file '%s'" % self.mbox_file_name)
        assert not self._locked
        self.mbox_file.close()


class Mbox(mailbox.UnixMailbox, LockableMboxMixin):
    """A mostly-read-only mbox with locking. The mbox content can only be
    modified by overwriting the entire underlying file."""

    def __init__(self, path):
        """Constructor for opening an existing 'mbox' mailbox.
        Extends constructor for mailbox.UnixMailbox()

        Named Arguments:
        path -- file name of the 'mbox' file to be opened
        """
        assert path
        fd = safe_open_existing(path)
        st = os.fstat(fd)
        self.original_atime = st.st_atime
        self.original_mtime = st.st_mtime
        self.starting_size = st.st_size
        self.mbox_file = os.fdopen(fd, "r+")
        self.mbox_file_name = path
        LockableMboxMixin.__init__(self, self.mbox_file, path)
        mailbox.UnixMailbox.__init__(self, self.mbox_file)

    def reset_timestamps(self):
        """Set the file timestamps to the original values"""
        assert self.original_atime
        assert self.original_mtime
        assert self.mbox_file_name
        os.utime(self.mbox_file_name, (self.original_atime,  \
            self.original_mtime))

    def get_size(self):
        """Return the current size of the mbox file on disk"""
        return os.path.getsize(self.mbox_file_name)

    def overwrite_with(self, mbox_filename):
        """Overwrite the mbox content with the content of the given mbox file."""
        fin = open(mbox_filename, "r")
        self.mbox_file.seek(0)
        shutil.copyfileobj(fin, self.mbox_file)
        self.mbox_file.truncate()


class ArchiveMbox(LockableMboxMixin):
    """Simple append-only access to the archive mbox. Entirely content-agnostic."""

    def __init__(self, path):
        fd = safe_open(path)
        self.mbox_file = os.fdopen(fd, "a")
        LockableMboxMixin.__init__(self, self.mbox_file, path)

    def append(self, filename):
        """Append the content of the given file to the mbox."""
        assert self._locked
        fin = open(filename, "r")
        oldsize = os.fstat(self.mbox_file.fileno()).st_size
        try:
            shutil.copyfileobj(fin, self.mbox_file)
        except:
            # We can safely abort here without data loss, because
            # we have not yet changed the original mailbox
            self.mbox_file.truncate(oldsize)
            raise
        fin.close()


class TempMbox:
    """A write-only temporary mbox. No locking methods."""

    def __init__(self, prefix=tempfile.template):
        """Creates a temporary mbox file."""
        fd, filename = tempfile.mkstemp(prefix=prefix)
        self.mbox_file_name = filename
        _stale.temp_mboxes.append(filename)
        self.mbox_file = os.fdopen(fd, "w")
        # an empty gzip file is not really empty (it contains the gzip header
        # and trailer), so we need to track manually if this mbox is empty
        self.empty = True

    def write(self, msg):
        """Write a rfc822 message object to the 'mbox' mailbox.
        If the rfc822 has no Unix 'From_' line, then one is constructed
        from other headers in the message.

        Arguments:
        msg -- rfc822 message object to be written

        """
        assert msg
        assert self.mbox_file

        self.empty = False
        vprint("saving message to file '%s'" % self.mbox_file_name)
        unix_from = msg.unixfrom
        if unix_from:
            msg_has_mbox_format = True
        else:
            msg_has_mbox_format = False
            unix_from = make_mbox_from(msg)
        self.mbox_file.write(unix_from)
        assert msg.headers
        self.mbox_file.writelines(msg.headers)
        self.mbox_file.write(os.linesep)

        # The following while loop is about twice as fast in
        # practice to 'self.mbox_file.writelines(msg.fp.readlines())'
        assert options.read_buffer_size > 0
        linebuf = ""
        while 1:
            body = msg.fp.read(options.read_buffer_size)
            if (not msg_has_mbox_format) and options.mangle_from:
                # Be careful not to break pattern matching
                splitindex = body.rfind(os.linesep)
                nicebody = linebuf + body[:splitindex]
                linebuf = body[splitindex:]
                body = from_re.sub('>From ', nicebody)
            if not body:
                break
            self.mbox_file.write(body)
        if not msg_has_mbox_format:
            self.mbox_file.write(os.linesep)

    def commit(self):
        """Sync the mbox file to disk."""
        self.mbox_file.flush()
        os.fsync(self.mbox_file.fileno())

    def close(self):
        """Close the mbox file"""
        vprint("closing file '%s'" % self.mbox_file_name)
        self.mbox_file.close()

    def saveas(self, filename):
        """Rename this temporary mbox file to the given name, making it
        permanent.  Emergency use only."""
        os.rename(self.mbox_file_name, filename)
        _stale.temp_mboxes.remove(retain.mbox_file_name)

    def remove(self):
        """Delete the temporary mbox file."""
        os.remove(self.mbox_file_name)
        _stale.temp_mboxes.remove(self.mbox_file_name)


class CompressedTempMbox(TempMbox):
    """A compressed version of a TempMbox."""

    def __init__(self, prefix=tempfile.template):
        TempMbox.__init__(self, prefix)
        self.raw_file = self.mbox_file
        self.mbox_file = gzip.GzipFile(mode="a", fileobj=self.mbox_file)
        # Workaround that GzipFile.close() isn't idempotent in Python < 2.6
        # (python issue #2959).  There is no GzipFile.closed, so we need a
        # replacement.
        self.gzipfile_closed = False

    def commit(self):
        """Finish gzip file and sync it to disk."""
        # This method is currently not used
        self.mbox_file.close()  # close GzipFile, writing gzip trailer
        self.gzipfile_closed = True
        self.raw_file.flush()
        os.fsync(self.raw_file.fileno())

    def close(self):
        """Close the gzip file."""
        if not self.gzipfile_closed:
            self.mbox_file.close()
        self.raw_file.close()


class IdentityCache:
    """Class used to remember Message-IDs and warn if they are seen twice"""
    seen_ids = {}
    mailbox_name = None

    def __init__(self, mailbox_name):
        """Constructor: takes the mailbox name as an argument"""
        assert mailbox_name
        self.mailbox_name = mailbox_name

    def warn_if_dupe(self, msg):
        """Print a warning message if the message has already appeared"""
        assert msg
        message_id = msg.get('Message-ID')
        assert message_id
        if self.seen_ids.has_key(message_id):
            user_warning("duplicate message id: '%s' in mailbox '%s'" % 
                (message_id, self.mailbox_name))
        self.seen_ids[message_id] = 1


# global class instances
options = Options()  # the run-time options object
_stale = StaleFiles() # remember what we have to delete on abnormal exit


def main(args = sys.argv[1:]):
    global _stale

    # this usage message is longer than 24 lines -- bad idea?
    usage = """Usage: %s [options] mailbox [mailbox...]
Moves old mail in IMAP, mbox, MH or maildir-format mailboxes to an mbox-format
mailbox compressed with gzip. 

Options are as follows:
  -d, --days=NUM        archive messages older than NUM days (default: %d)
  -D, --date=DATE       archive messages older than DATE
  -o, --output-dir=DIR  directory to store archives (default: same as original)
  -P, --pwfile=FILE     file to read imap password from (default: None)
  -F, --filter-append=STRING  append arbitrary string to the IMAP filter string
  -s, --suffix=NAME     suffix for archive filename (default: '%s')
  -S, --size=NUM        only archive messages NUM bytes or larger
  -n, --dry-run         don't write to anything - just show what would be done
  -u, --preserve-unread never archive unread messages
      --dont-mangle     do not mangle From_ in message bodies
      --delete          delete rather than archive old mail (use with caution!)
      --copy            copy rather than archive old mail 
      --include-flagged messages flagged important can also be archived
      --all             archive all messages 
      --no-compress     do not compress archives with gzip
      --warn-duplicate  warn about duplicate Message-IDs in the same mailbox
  -v, --verbose         report lots of extra debugging information
      --debug-imap=NUM  set IMAP debugging output level (0 is none)
  -q, --quiet           quiet mode - print no statistics (suitable for crontab)
  -V, --version         display version information
  -h, --help            display this message

Example: %s linux-kernel
  This will move all messages older than %s days to a 'mbox' mailbox called 
  'linux-kernel_archive.gz', deleting them from the original 'linux-kernel'
  mailbox. If the 'linux-kernel_archive.gz' mailbox already exists, the 
  newly archived messages are appended.

To archive IMAP mailboxes, format your mailbox argument like this:
  imap://username:password@server/mailbox
  (substitute 'imap' with 'imaps' for an SSL connection)

Website: http://archivemail.sourceforge.net/ """ %   \
    (options.script_name, options.days_old_max, options.archive_suffix,
    options.script_name, options.days_old_max)

    args = options.parse_args(args, usage)
    if len(args) == 0:
        print usage
        sys.exit(1)

    options.sanity_check()

    for mailbox_path in args:
        archive(mailbox_path)


######## errors and debug ##########

def vprint(string):
    """Print the string argument if we are in verbose mode"""
    if options.verbose:
        print string


def unexpected_error(string):
    """Print the string argument, a 'shutting down' message and abort.  Raise
    UnexpectedErrors if archivemail is run as a module. This function never
    returns."""
    if not __name__ == '__main__':
        raise UnexpectedError(string)
    sys.stderr.write("%s: %s\n" % (options.script_name, string))
    sys.stderr.write("%s: unexpected error encountered - shutting down\n" % 
        options.script_name)
    sys.exit(1)


def user_error(string):
    """Print the string argument and abort. Raise UserError if archivemail is
    run as a module. This function never returns."""
    if not __name__ == '__main__':
        raise UserError(string)
    sys.stderr.write("%s: %s\n" % (options.script_name, string))
    sys.exit(1)


def user_warning(string):
    """Print the string argument"""
    sys.stderr.write("%s: Warning - %s\n" % (options.script_name, string))

########### operations on a message ############

def make_mbox_from(message):
    """Return a string suitable for use as a 'From_' mbox header for the
    message.

    Arguments:
    message -- the rfc822 message object

    """
    assert message
    address = guess_return_path(message)
    time_message = guess_delivery_time(message)
    date = time.localtime(time_message)
    assert date
    date_string = time.asctime(date)
    mbox_from = "From %s %s\n" % (address, date_string)
    return mbox_from


def guess_return_path(message):
    """Return a guess at the Return Path address of an rfc822 message"""
    assert message

    for header in ('Return-path', 'From'):
        address_header = message.get(header)
        if address_header:
            (name, address) = rfc822.parseaddr(address_header)
            if address:
                return address
    # argh, we can't find any valid 'Return-path' guesses - just 
    # just use the current unix username like mutt does
    login = pwd.getpwuid(os.getuid())[0]
    assert login
    return login


def guess_delivery_time(message):
    """Return a guess at the delivery date of an rfc822 message""" 
    assert message
    # try to guess the delivery date from various headers
    # get more desparate as we go through the array
    for header in 'Delivery-date', 'Received', 'Resent-Date', 'Date':
        try: 
            if header == 'Received': 
                # This should be good enough for almost all headers in the wild; 
                # if we're guessing wrong, parsedate_tz() will fail graciously. 
                token = message.getrawheader(header).rsplit(';', 1)[-1]
            else: 
                token = message.get(header)
            date = rfc822.parsedate_tz(token)
            if date:
                time_message = rfc822.mktime_tz(date)
                vprint("using valid time found from '%s' header" % header)
                return time_message
        except (AttributeError, IndexError, ValueError, OverflowError): pass
    # as a second-last resort, try the date from the 'From_' line (ugly)
    # this will only work from a mbox-format mailbox
    if (message.unixfrom):
        # Hmm. This will break with full-blown RFC 2822 addr-spec's. 
        header = message.unixfrom.split(None, 2)[-1]
        # Interpret no timezone as localtime
        date = rfc822.parsedate_tz(header)
        if date:
            try:
                time_message = rfc822.mktime_tz(date)
                vprint("using valid time found from unix 'From_' header")
                return time_message
            except (ValueError, OverflowError): pass
    # the headers have no valid dates -- last resort, try the file timestamp
    # this will not work for mbox mailboxes
    try:
        file_name = get_filename(message)
    except AttributeError:
        # we are looking at a 'mbox' mailbox - argh! 
        # Just return the current time - this will never get archived :(
        vprint("no valid times found at all -- using current time!")
        return time.time()
    if not os.path.isfile(file_name):
        unexpected_error("mailbox file name '%s' has gone missing" % \
            file_name)    
    time_message = os.path.getmtime(file_name)
    vprint("using valid time found from '%s' last-modification time" % \
        file_name)
    return time_message
   

def add_status_headers(message):
    """
    Add Status and X-Status headers to a message from a maildir mailbox.

    Maildir messages store their information about being read/replied/etc in
    the suffix of the filename rather than in Status and X-Status headers in
    the message. In order to archive maildir messages into mbox format, it is
    nice to preserve this information by putting it into the status headers.

    """
    status = ""
    x_status = ""
    file_name = get_filename(message)
    match = re.search(":2,(.+)$", file_name)
    if match:
        flags = match.group(1)
        for flag in flags: 
            if flag == "D": # (draft): the user considers this message a draft
                pass # does this make any sense in mbox? 
            elif flag == "F": # (flagged): user-defined 'important' flag
                x_status = x_status + "F"
            elif flag == "R": # (replied): the user has replied to this message
                x_status = x_status + "A"
            elif flag == "S": # (seen): the user has viewed this message
                status = status + "R"
            elif flag == "T": # (trashed): user has moved this message to trash
                pass # is this Status: D ? 
            else:
                pass # no whingeing here, although it could be a good experiment

    # files in the maildir 'cur' directory are no longer new,
    # they are the same as messages with 'Status: O' headers in mbox
    last_dir = os.path.basename(os.path.dirname(file_name))
    if last_dir == "cur":
        status = status + "O" 

    # Overwrite existing 'Status' and 'X-Status' headers.  They add no value in
    # maildirs, and we better don't listen to them.
    if status:
        vprint("converting maildir status into Status header '%s'" % status)
        message['Status'] = status
    else: 
        del message['Status']
    if x_status:
        vprint("converting maildir status into X-Status header '%s'" % x_status)
        message['X-Status'] = x_status
    else: 
        del message['X-Status']

def add_status_headers_imap(message, flags):
    """Add Status and X-Status headers to a message from an imap mailbox."""
    status = ""
    x_status = ""
    for flag in flags: 
        if flag == "\\Draft": # (draft): the user considers this message a draft
            pass # does this make any sense in mbox? 
        elif flag == "\\Flagged": # (flagged): user-defined 'important' flag
            x_status = x_status + "F"
        elif flag == "\\Answered": # (replied): the user has replied to this message
            x_status = x_status + "A"
        elif flag == "\\Seen": # (seen): the user has viewed this message
            status = status + "R"
        elif flag == "\\Deleted": # (trashed): user has moved this message to trash
            pass # is this Status: D ? 
        else:
            pass # no whingeing here, although it could be a good experiment
    if not "\\Recent" in flags:
        status = status + "O" 

    # As with maildir folders, overwrite Status and X-Status headers 
    # if they exist.
    vprint("converting imap status (%s)..." % " ".join(flags))
    if status:
        vprint("generating Status header '%s'" % status)
        message['Status'] = status
    else: 
        vprint("not generating Status header")
        del message['Status']
    if x_status:
        vprint("generating X-Status header '%s'" % x_status)
        message['X-Status'] = x_status
    else: 
        vprint("not generating X-Status header")
        del message['X-Status']

def is_flagged(message):
    """return true if the message is flagged important, false otherwise"""
    # MH and mbox mailboxes use the 'X-Status' header to indicate importance
    x_status = message.get('X-Status')
    if x_status and re.search('F', x_status):
        vprint("message is important (X-Status header='%s')" % x_status)
        return 1
    file_name = None
    try:
        file_name = get_filename(message)
    except AttributeError:
        pass
    # maildir mailboxes use the filename suffix to indicate flagged status
    if file_name and re.search(":2,.*F.*$", file_name):
        vprint("message is important (filename info has 'F')")
        return 1
    vprint("message is not flagged important")
    return 0


def is_unread(message):
    """return true if the message is unread, false otherwise"""
    # MH and mbox mailboxes use the 'Status' header to indicate read status
    status = message.get('Status')
    if status and re.search('R', status):
        vprint("message has been read (status header='%s')" % status)
        return 0
    file_name = None
    try:
        file_name = get_filename(message)
    except AttributeError:
        pass
    # maildir mailboxes use the filename suffix to indicate read status
    if file_name and re.search(":2,.*S.*$", file_name):
        vprint("message has been read (filename info has 'S')")
        return 0
    vprint("message is unread")
    return 1


def sizeof_message(message):
    """Return size of message in bytes (octets)."""
    assert message
    file_name = None
    message_size = None
    try:
        file_name = get_filename(message)
    except AttributeError:
        pass
    if file_name:
        # with maildir and MH mailboxes, we can just use the file size
        message_size = os.path.getsize(file_name)
    else:
        # with mbox mailboxes, not so easy
        message_size = 0
        if message.unixfrom:
            message_size = message_size + len(message.unixfrom)
        for header in message.headers:
            message_size = message_size + len(header)
        message_size = message_size + 1 # the blank line after the headers
        start_offset = message.fp.tell()
        message.fp.seek(0, 2) # seek to the end of the message
        end_offset = message.fp.tell()
        message.rewindbody()
        message_size = message_size + (end_offset - start_offset)
    return message_size

def is_smaller(message, size):
    """Return true if the message is smaller than size bytes, false otherwise"""
    assert message
    assert size > 0
    message_size = sizeof_message(message) 
    if message_size < size:
        vprint("message is too small (%d bytes), minimum bytes : %d" % \
            (message_size, size))
        return 1
    else:
        vprint("message is not too small (%d bytes), minimum bytes: %d" % \
            (message_size, size))
        return 0


def should_archive(message):
    """Return true if we should archive the message, false otherwise"""
    if options.archive_all:
        return 1
    old = 0
    time_message = guess_delivery_time(message)
    if options.date_old_max == None:
        old = is_older_than_days(time_message, options.days_old_max)
    else:
        old = is_older_than_time(time_message, options.date_old_max)

    # I could probably do this in one if statement, but then I wouldn't
    # understand it. 
    if not old:
        return 0
    if not options.include_flagged and is_flagged(message):
        return 0
    if options.min_size and is_smaller(message, options.min_size):
        return 0
    if options.preserve_unread and is_unread(message):
        return 0
    return 1
        
    
def is_older_than_time(time_message, max_time):
    """Return true if a message is older than the specified time,
    false otherwise.

    Arguments:
    time_message -- the delivery date of the message measured in seconds
                    since the epoch
    max_time -- maximum time allowed for message
       
    """
    days_old = (max_time - time_message) / 24 / 60 / 60
    if time_message < max_time:
        vprint("message is %.2f days older than the specified date" % days_old)
        return 1
    vprint("message is %.2f days younger than the specified date" % \
        abs(days_old))
    return 0


def is_older_than_days(time_message, max_days):
    """Return true if a message is older than the specified number of days,
    false otherwise.

    Arguments:
    time_message -- the delivery date of the message measured in seconds
                    since the epoch
    max_days -- maximum number of days before message is considered old
    """
    time_now = time.time()
    if time_message > time_now:
        vprint("warning: message has date in the future")
        return 0
    secs_old_max = (max_days * 24 * 60 * 60)
    days_old = (time_now - time_message) / 24 / 60 / 60
    vprint("message is %.2f days old" % days_old)
    if ((time_message + secs_old_max) < time_now):
        return 1
    return 0

def build_imap_filter():
    """Return an imap filter string"""

    imap_filter = []
    old = 0
    if options.date_old_max == None:
        time_now = time.time()
        secs_old_max = (options.days_old_max * 24 * 60 * 60)
        time_old = time.gmtime(time_now - secs_old_max)
    else:
        time_old = time.gmtime(options.date_old_max)
    time_str = time.strftime('%d-%b-%Y', time_old)
    imap_filter.append("BEFORE %s" % time_str)

    if not options.include_flagged:
        imap_filter.append("UNFLAGGED")
    if options.min_size:
        imap_filter.append("LARGER %d" % options.min_size)
    if options.preserve_unread:
        imap_filter.append("SEEN")
    if options.filter_append:
        imap_filter.append(options.filter_append)

    return '(' + string.join(imap_filter, ' ') + ')'

###############  mailbox operations ###############

def archive(mailbox_name):
    """Archives a mailbox.

    Arguments:
    mailbox_name -- the filename/dirname/url of the mailbox to be archived
    """
    assert mailbox_name

    # strip any trailing slash (we could be archiving a maildir or MH format
    # mailbox and somebody was pressing <tab> in bash) - we don't want to use
    # the trailing slash in the archive name
    mailbox_name = mailbox_name.rstrip("/")
    assert mailbox_name

    set_signal_handlers()
    os.umask(077) # saves setting permissions on mailboxes/tempfiles

    final_archive_name = make_archive_name(mailbox_name)
    vprint("archiving '%s' to '%s' ..." % (mailbox_name, final_archive_name))
    check_archive(final_archive_name)
    dest_dir = os.path.dirname(final_archive_name)
    if not dest_dir:
        dest_dir = os.getcwd()
    check_sane_destdir(dest_dir)
    is_imap = urlparse.urlparse(mailbox_name)[0] in ('imap', 'imaps')
    if not is_imap:
        # Check if the mailbox exists, and refuse to mess with other people's
        # stuff
        try:
            fuid = os.stat(mailbox_name).st_uid
        except OSError, e:
            user_error(str(e))
        else:
            if fuid != os.getuid():
                user_error("'%s' is owned by someone else!" % mailbox_name)

    old_temp_dir = tempfile.tempdir
    try:
        # create a temporary directory for us to work in securely
        tempfile.tempdir = None
        new_temp_dir = tempfile.mkdtemp('archivemail')
        assert new_temp_dir
        _stale.temp_dir = new_temp_dir
        tempfile.tempdir = new_temp_dir
        vprint("set tempfile directory to '%s'" % new_temp_dir)

        if is_imap:
            vprint("guessing mailbox is of type: imap(s)")
            _archive_imap(mailbox_name, final_archive_name)
        elif os.path.isfile(mailbox_name):
            vprint("guessing mailbox is of type: mbox")
            _archive_mbox(mailbox_name, final_archive_name)
        elif os.path.isdir(mailbox_name):
            cur_path = os.path.join(mailbox_name, "cur")
            new_path = os.path.join(mailbox_name, "new")
            if os.path.isdir(cur_path) and os.path.isdir(new_path):
                vprint("guessing mailbox is of type: maildir")
                _archive_dir(mailbox_name, final_archive_name, "maildir")
            else:
                vprint("guessing mailbox is of type: MH")
                _archive_dir(mailbox_name, final_archive_name, "mh")
        else:
            user_error("'%s' is not a normal file or directory" % mailbox_name)

        # remove our special temp directory - hopefully empty
        os.rmdir(new_temp_dir)
        _stale.temp_dir = None

    finally:
        tempfile.tempdir = old_temp_dir
        clean_up()

def _archive_mbox(mailbox_name, final_archive_name):
    """Archive a 'mbox' style mailbox - used by archive_mailbox()

    Arguments:
    mailbox_name -- the filename/dirname of the mailbox to be archived
    final_archive_name -- the filename of the 'mbox' mailbox to archive
                          old messages to - appending if the archive 
                          already exists
    """
    assert mailbox_name
    assert final_archive_name
    stats = Stats(mailbox_name, final_archive_name)
    cache = IdentityCache(mailbox_name)
    original = Mbox(path=mailbox_name)
    if options.dry_run or options.copy_old_mail:
        retain = None
    else:
        retain = TempMbox(prefix="retain")
    archive = prepare_temp_archive()

    original.lock()
    msg = original.next()
    if not msg and (original.starting_size > 0):
        user_error("'%s' is not a valid mbox-format mailbox" % mailbox_name)
    while (msg):
        msg_size = sizeof_message(msg)
        stats.another_message(msg_size)
        vprint("processing message '%s'" % msg.get('Message-ID'))
        if options.warn_duplicates:
            cache.warn_if_dupe(msg)             
        if should_archive(msg):
            stats.another_archived(msg_size)
            if options.delete_old_mail:
                vprint("decision: delete message")
            else:
                vprint("decision: archive message")
                if archive:
                    archive.write(msg)
        else:
            vprint("decision: retain message")
            if retain:
                retain.write(msg)
        msg = original.next()
    vprint("finished reading messages") 
    if original.starting_size != original.get_size():
        unexpected_error("the mailbox '%s' changed size during reading!" % \
           mailbox_name)         
    # Write the new archive before modifying the mailbox, to prevent
    # losing data if something goes wrong
    commit_archive(archive, final_archive_name)
    if retain:
        pending_changes = original.mbox_file.tell() != retain.mbox_file.tell()
        if pending_changes:
            retain.commit()
            retain.close()
            vprint("writing back changed mailbox '%s'..." % \
                    original.mbox_file_name)
            # Prepare for recovery on error.
            # FIXME: tempfile.tempdir is our nested dir.
            saved_name = "%s/%s.%s.%s-%s-%s" % \
                (tempfile.tempdir, options.script_name,
                    os.path.basename(original.mbox_file_name),
                    socket.gethostname(), os.getuid(),
                    os.getpid())
            try:
                original.overwrite_with(retain.mbox_file_name)
                original.commit()
            except:
                retain.saveas(saved_name)
                print "Error writing back changed mailbox; saved good copy to " \
                        "%s" % saved_name
                raise
        else:
            retain.close()
            vprint("no changes to mbox '%s'" %  original.mbox_file_name)
        retain.remove()
    original.unlock()
    original.close()
    original.reset_timestamps() # Minor race here; mutt has this too.
    if not options.quiet:
        stats.display()


def _archive_dir(mailbox_name, final_archive_name, type):
    """Archive a 'maildir' or 'MH' style mailbox - used by archive_mailbox()"""
    assert mailbox_name
    assert final_archive_name
    assert type
    stats = Stats(mailbox_name, final_archive_name)
    delete_queue = []

    if type == "maildir":
        original = mailbox.Maildir(mailbox_name)
    elif type == "mh":
        original = mailbox.MHMailbox(mailbox_name)
    else:
        unexpected_error("unknown type: %s" % type)        
    cache = IdentityCache(mailbox_name)
    archive = prepare_temp_archive()

    for msg in original:
        if not msg: 
            vprint("ignoring invalid message '%s'" % get_filename(msg))
            continue
        msg_size = sizeof_message(msg)
        stats.another_message(msg_size)
        vprint("processing message '%s'" % msg.get('Message-ID'))
        if options.warn_duplicates:
            cache.warn_if_dupe(msg)             
        if should_archive(msg):
            stats.another_archived(msg_size)
            if options.delete_old_mail:
                vprint("decision: delete message")
            else:
                vprint("decision: archive message")
                if archive:
                    if type == "maildir":
                        add_status_headers(msg)
                    archive.write(msg)
            if not options.dry_run and not options.copy_old_mail:
                delete_queue.append(get_filename(msg)) 
        else:
            vprint("decision: retain message")
    vprint("finished reading messages") 
    # Write the new archive before modifying the mailbox, to prevent
    # losing data if something goes wrong
    commit_archive(archive, final_archive_name)
    for file_name in delete_queue:
        vprint("removing original message: '%s'" % file_name)
        try: os.remove(file_name)
        except OSError, e:
            if e.errno != errno.ENOENT: raise
    if not options.quiet:
        stats.display()

def _archive_imap(mailbox_name, final_archive_name):
    """Archive an imap mailbox - used by archive_mailbox()"""
    assert mailbox_name
    assert final_archive_name
    import imaplib
    import cStringIO
    import getpass

    vprint("Setting imaplib.Debug = %d" % options.debug_imap)
    imaplib.Debug = options.debug_imap
    archive = None
    stats = Stats(mailbox_name, final_archive_name)
    cache = IdentityCache(mailbox_name)
    imap_str = mailbox_name[mailbox_name.find('://') + 3:]
    imap_username, imap_password, imap_server, imap_folder = \
        parse_imap_url(imap_str)
    if not imap_password: 
        if options.pwfile:
            imap_password = open(options.pwfile).read().rstrip()
        else:
            if (not os.isatty(sys.stdin.fileno())) or options.quiet:
                unexpected_error("No imap password specified")
            imap_password = getpass.getpass('IMAP password: ')

    is_ssl = mailbox_name[:5].lower() == 'imaps'
    if is_ssl: 
        vprint("establishing secure connection to server %s" % imap_server)
        imap_srv = imaplib.IMAP4_SSL(imap_server)
    else:
        vprint("establishing connection to server %s" % imap_server)
        imap_srv = imaplib.IMAP4(imap_server)
    if "AUTH=CRAM-MD5" in imap_srv.capabilities: 
        vprint("authenticating (cram-md5) to server as %s" % imap_username)
        result, response = imap_srv.login_cram_md5(imap_username, imap_password)
    elif not "LOGINDISABLED" in imap_srv.capabilities: 
        vprint("logging in to server as %s" % imap_username)
        result, response = imap_srv.login(imap_username, imap_password)
    else: 
        user_error("imap server %s has login disabled (hint: "
                             "try ssl/imaps)" % imap_server)

    imap_smart_select(imap_srv, imap_folder)
    total_msg_count = int(imap_srv.response("EXISTS")[1][0])
    vprint("folder has %d message(s)" % total_msg_count)

    # IIUIC the message sequence numbers are stable for the whole session, since
    # we just send SEARCH, FETCH and STORE commands, which should prevent the
    # server from sending untagged EXPUNGE responses -- see RFC 3501 (IMAP4rev1)
    # 7.4.1 and RFC 2180 (Multi-Accessed Mailbox Practice).
    # Worst thing should be that we bail out FETCHing a message that has been
    # deleted.

    if options.archive_all:
        message_list = [str(n) for n in range(1, total_msg_count+1)]
    else:
        imap_filter = build_imap_filter()
        vprint("imap filter: '%s'" % imap_filter)
        vprint("searching messages matching criteria")
        result, response = imap_srv.search(None, imap_filter)
        if result != 'OK': unexpected_error("imap search failed; server says '%s'" %
            response[0])
        # response is a list with a single item, listing message sequence numbers
        # like ['1 2 3 1016'] 
        message_list = response[0].split()
        vprint("%d messages are matching filter" % len(message_list))

    # First, gather data for the statistics.
    if total_msg_count > 0:
        vprint("fetching size of messages...")
        result, response = imap_srv.fetch('1:*', '(RFC822.SIZE)')
        if result != 'OK': unexpected_error("Failed to fetch message sizes; "
            "server says '%s'" % response[0])
        # response is a list with entries like '1016 (RFC822.SIZE 3118)',
        # where the first number is the message sequence number, the second is
        # the size.
        for x in response:
            m = imapsize_re.match(x)
            msn, msg_size = m.group('msn'), int(m.group('size'))
            stats.another_message(msg_size)
            if msn in message_list:
                stats.another_archived(msg_size)

    if not options.dry_run:
        if not options.delete_old_mail:
            archive = prepare_temp_archive()
            vprint("fetching messages...")
            for msn in message_list:
                # Fetching message flags and body together always finds \Seen
                # set.  To check \Seen, we must fetch the flags first. 
                result, response = imap_srv.fetch(msn, '(FLAGS)')
                if result != 'OK': unexpected_error("Failed to fetch message "
                        "flags; server says '%s'" % response[0])
                msg_flags = imaplib.ParseFlags(response[0])
                result, response = imap_srv.fetch(msn, '(RFC822)')
                if result != 'OK': unexpected_error("Failed to fetch message; "
                    "server says '%s'" % response[0])
                msg_str = response[0][1].replace("\r\n", os.linesep)
                msg = rfc822.Message(cStringIO.StringIO(msg_str))
                vprint("processing message '%s'" % msg.get('Message-ID'))
                add_status_headers_imap(msg, msg_flags)
                if options.warn_duplicates:
                    cache.warn_if_dupe(msg)             
                archive.write(msg)
            commit_archive(archive, final_archive_name)
        if not options.copy_old_mail: 
            vprint("Deleting %s messages" % len(message_list))
            # do not delete more than a certain number of messages at a time,
            # because the command length is limited. This avoids that servers
            # terminate the connection with EOF or TCP RST.
            max_delete = 100
            for i in range(0, len(message_list), max_delete):
                result, response = imap_srv.store( \
                    string.join(message_list[i:i+max_delete], ','),
                    '+FLAGS.SILENT', '\\Deleted')
                if result != 'OK': unexpected_error("Error while deleting "
                    "messages; server says '%s'" % response[0])
    vprint("Closing mailbox and terminating connection.")
    imap_srv.close()
    imap_srv.logout()
    if not options.quiet:
        stats.display()
    

###############  IMAP  functions  ###############


def parse_imap_url(url): 
    """Parse IMAP URL and return username, password (if appliciable), servername
    and foldername."""

    def split_qstr(string, delim): 
        """Split string once at delim, keeping quoted substring intact.
        Strip and unescape quotes where necessary."""
        rm = re.match(r'"(.+?(?<!\\))"(.)(.*)', string)
        if rm:
            a, d, b = rm.groups()
            if not d == delim: 
                raise ValueError
            a = a.replace('\\"', '"')
        else:
            a, b = string.split(delim, 1)
        return a, b

    password = None
    try: 
        if options.pwfile: 
            username, url = split_qstr(url, '@')
        else: 
            try:
                username, url = split_qstr(url, ':')
            except ValueError: 
                # request password interactively later
                username, url = split_qstr(url, '@')
            else: 
                password, url = split_qstr(url, '@')
        server, folder = url.split('/', 1)
    except ValueError:
        unexpected_error("Invalid IMAP connection string")
    return username, password, server, folder


def imap_getdelim(imap_server): 
    """Return the IMAP server's hierarchy delimiter. Assumes there is only one."""
    # This function will break if the LIST reply doesn't meet our expectations. 
    # Imaplib and IMAP itself are both little beasts, and I do not know how
    # fragile this function will be in the wild.
    try: 
        result, response = imap_server.list(pattern='""')
    except ValueError:
        # Stolen from offlineimap: 
        # Some buggy IMAP servers do not respond well to LIST "" ""
        # Work around them.
        result, response = imap_server.list(pattern='%')
    if result != 'OK': unexpected_error("Error listing directory; "
        "server says '%s'" % response[0])

    # Response should be a list of strings like 
    # '(\\Noselect \\HasChildren) "." "boxname"'
    # We parse only the first list item and just grab the delimiter. 
    m = re.match(r'\([^\)]*\) (?P<delim>"."|NIL)', response[0])
    if not m: 
        unexpected_error("imap_getdelim(): cannot parse '%s'" % response[0])
    delim = m.group('delim').strip('"')
    vprint("Found mailbox hierarchy delimiter: '%s'" % delim)
    if delim == "NIL": 
        return None
    return delim


def imap_get_namespace(srv):
    """Return the IMAP namespace prefixes and hierarchy delimiters."""
    assert 'NAMESPACE' in srv.capabilities
    result, response = srv.namespace()
    if result != 'OK': 
        unexpected_error("Cannot retrieve IMAP namespace; server says: '%s'" 
            % response[0])
    vprint("NAMESPACE response: %s" % repr(response[0]))
    # Typical response is e.g.
    # ['(("INBOX." ".")) NIL (("#shared." ".")("shared." "."))'] or
    # ['(("" ".")) NIL NIL'], see RFC 2342.
    # Make a reasonable guess parsing this beast. 
    try:
        m = re.match(r'\(\("([^"]*)" (?:"(.)"|NIL)', response[0])
        nsprefix, hdelim = m.groups()
    except:
        print "Cannot parse IMAP NAMESPACE response %s" % repr(response)
        raise
    return nsprefix, hdelim


def imap_smart_select(srv, mailbox): 
    """Select the given mailbox on the IMAP server, correcting an invalid
    mailbox path if possible."""
    mailbox = imap_find_mailbox(srv, mailbox)
    roflag = options.dry_run or options.copy_old_mail
    # Work around python bug #1277098 (still pending in python << 2.5)
    if not roflag: 
        roflag = None
    if roflag:
        vprint("examining imap folder '%s' read-only" % mailbox)
    else:
        vprint("selecting imap folder '%s'" % mailbox)
    result, response = srv.select(mailbox, roflag)
    if result != 'OK':
        unexpected_error("selecting '%s' failed; server says: '%s'." \
                % (mailbox, response[0]))
    if not roflag: 
        # Sanity check that we don't silently fail to delete messages. 
        # As to the following indices: IMAP4.response(key) returns 
        # a tuple (key, ['<all_items>']) if the key is found, (key, [None])
        # otherwise.  Imaplib just *loves* to nest trivial lists!  
        permflags = srv.response("PERMANENTFLAGS")[1][0]
        if permflags: 
            permflags = permflags.strip('()').lower().split()
            if not '\\deleted' in permflags: 
                unexpected_error("Server doesn't allow deleting messages in " \
                        "'%s'." % mailbox)
        elif "IMAP4REV1" in srv.capabilities: 
            vprint("Suspect IMAP4rev1 server, doesn't send PERMANENTFLAGS " \
                    "upon SELECT")


def imap_find_mailbox(srv, mailbox):
    """Find the given mailbox on the IMAP server, correcting an invalid
    mailbox path if possible.  Return the found mailbox name.""" 
    for curbox in imap_guess_mailboxnames(srv, mailbox): 
        vprint("Looking for mailbox '%s'..." % curbox)
        result, response = srv.list(pattern=curbox)
        if result != 'OK': 
            unexpected_error("LIST command failed; " \
                "server says: '%s'" % response[0])
        # Say we queried for the mailbox "foo". 
        # Upon success, response is e.g. ['(\\HasChildren) "." "foo"'].
        # Upon failure, response is [None].  Funky imaplib!
        if response[0] != None: 
            break
    else: 
        user_error("Cannot find mailbox '%s' on server." % mailbox)
    vprint("Found mailbox '%s'" % curbox)
    # Catch \NoSelect here to avoid misleading errors later. 
    m = re.match(r'\((?P<attrs>[^\)]*)\)', response[0])
    if '\\noselect' in m.group('attrs').lower().split(): 
        user_error("Server indicates that mailbox '%s' is not selectable" \
            % curbox)
    return curbox


def imap_guess_mailboxnames(srv, mailbox): 
    """Return a list of possible real IMAP mailbox names in descending order
    of preference, compiled by prepending an IMAP namespace prefix if necessary,
    and by translating hierarchy delimiters."""
    if 'NAMESPACE' in srv.capabilities: 
        nsprefix, hdelim = imap_get_namespace(srv)
    else: 
        vprint("Server doesn't support NAMESPACE command.")
        nsprefix = ""
        hdelim = imap_getdelim(srv)
    vprint("IMAP namespace prefix: '%s', hierarchy delimiter: '%s'" % \
            (nsprefix, hdelim))
    if mailbox.startswith(nsprefix):
        boxnames = [mailbox]
    else:
        boxnames = [nsprefix + mailbox]
    if os.path.sep in mailbox and hdelim is not None:
        mailbox = mailbox.replace(os.path.sep, hdelim)
        if mailbox.startswith(nsprefix):
            boxnames.append(mailbox)
        if nsprefix: 
            boxnames.append(nsprefix + mailbox)
    return boxnames


###############  misc  functions  ###############


def set_signal_handlers():
    """set signal handlers to clean up temporary files on unexpected exit"""
    # Make sure we clean up nicely - we don't want to leave stale dotlock
    # files about if something bad happens to us. This is quite
    # important, even though procmail will delete stale files after a while.
    signal.signal(signal.SIGHUP, clean_up_signal)   # signal 1
    # SIGINT (signal 2) is handled as a python exception
    signal.signal(signal.SIGQUIT, clean_up_signal)  # signal 3
    signal.signal(signal.SIGTERM, clean_up_signal)  # signal 15


def clean_up():
    """Delete stale files"""
    vprint("cleaning up ...")
    _stale.clean()


def clean_up_signal(signal_number, stack_frame):
    """Delete stale files -- to be registered as a signal handler.

    Arguments:
    signal_number -- signal number of the terminating signal
    stack_frame -- the current stack frame
    
    """
    # this will run the above clean_up(), since unexpected_error()
    # will abort with sys.exit() and clean_up will be registered 
    # at this stage
    unexpected_error("received signal %s" % signal_number)

def prepare_temp_archive():
    """Create temporary archive mbox."""
    if options.dry_run or options.delete_old_mail:
        return None
    if options.no_compress:
        return TempMbox()
    else:
        return CompressedTempMbox()

def commit_archive(archive, final_archive_name):
    """Finalize temporary archive and write it to its final destination."""
    if not options.no_compress:
        final_archive_name = final_archive_name + '.gz'
    if archive:
        archive.close()
        if not archive.empty:
            final_archive = ArchiveMbox(final_archive_name)
            final_archive.lock()
            try:
                final_archive.append(archive.mbox_file_name)
                final_archive.commit()
            finally:
                final_archive.unlock()
                final_archive.close()
        archive.remove()

def make_archive_name(mailbox_name):
    """Derive archive name and (relative) path from the mailbox name."""
    # allow the user to embed time formats such as '%B' in the suffix string
    if options.date_old_max == None:
        parsed_suffix_time = time.time() - options.days_old_max*24*60*60
    else:
        parsed_suffix_time = options.date_old_max
    parsed_suffix = time.strftime(options.archive_suffix,
        time.localtime(parsed_suffix_time))

    if re.match(r'imaps?://', mailbox_name.lower()):
        mailbox_name = mailbox_name.rsplit('/', 1)[-1]
    final_archive_name = mailbox_name + parsed_suffix
    if options.output_dir:
        final_archive_name = os.path.join(options.output_dir,
                os.path.basename(final_archive_name))
    return final_archive_name

def check_sane_destdir(dir):
    """Do a very primitive check if the given directory looks like a reasonable
    destination directory and bail out if it doesn't."""
    assert dir
    if not os.path.isdir(dir):
        user_error("output directory does not exist: '%s'" % dir)
    if not os.access(dir, os.W_OK):
        user_error("no write permission on output directory: '%s'" % dir)

def check_archive(archive_name):
    """Check if existing archive files are (not) compressed as expected."""
    compressed_archive = archive_name + ".gz"
    if options.no_compress:
        if os.path.isfile(compressed_archive):
            user_error("There is already a file named '%s'!\n"
                "Have you been previously compressing this archive?\n"
                "You probably should uncompress it manually, and try running me "
                "again." % compressed_archive)
    elif os.path.isfile(archive_name):
        user_error("There is already a file named '%s'!\n"
            "Have you been reading this archive?\n"
            "You probably should re-compress it manually, and try running me "
            "again." % archive_name)

def nice_size_str(size):
    """Return given size in bytes as '12kB', '1.2MB'"""
    kb = size / 1024.0
    mb = kb / 1024.0
    if mb >= 1.0: return str(round(mb, 1)) + 'MB'
    if kb >= 1.0: return str(round(kb)) + 'kB'
    return str(size) + 'B'


def get_filename(msg): 
    """If the given rfc822.Message can be identified with a file (no mbox),
    return the filename, otherwise raise AttributeError."""
    try:
        return msg.fp.name
    except AttributeError:
        # Ugh, that's ugly.  msg.fp is not a plain file, it may be an 
        # instance of 
        # a. mailbox._Subfile 
        #    (msg from mailbox.UnixMailbox, Python <= 2.4) 
        #    File object is msg.fp.fp, we don't want that
        # b. mailbox._PartialFile, subclass of mailbox._ProxyFile
        #    (msg from mailbox.UnixMailbox, Python >= 2.5)
        #    File object is msg.fp._file, we don't want that
        # c. mailbox._ProxyFile
        #    (msg from mailbox.Maildir, Python >= 2.5)
        #    File object is msg.fp._file, we do want that.
        if msg.fp.__class__ == mailbox._ProxyFile: 
            assert hasattr(mailbox, "_PartialFile")
            return msg.fp._file.name
        raise

def safe_open_create(filename):
    """Create and open a file in a NFSv2-safe way, and return a r/w file descriptor.
    The new file is created with mode 600."""
    # This is essentially a simplified version of the dotlocking function.
    vprint("Creating file '%s'" % filename)
    dir, basename = os.path.split(filename)
    # We rely on tempfile.mkstemp to create files safely and with 600 mode.
    fd, pre_name = tempfile.mkstemp(prefix=basename+".pre-", dir=dir)
    try:
        try:
            os.link(pre_name, filename)
        except OSError, e:
            if os.fstat(fd).st_nlink == 2:
                pass
            else:
                raise
    finally:
        os.unlink(pre_name)
    return fd

def safe_open_existing(filename):
    """Safely open an existing file, and return a r/w file descriptor."""
    lst = os.lstat(filename)
    if stat.S_ISLNK(lst.st_mode):
        unexpected_error("file '%s' is a symlink." % filename)
    fd = os.open(filename, os.O_RDWR)
    fst = os.fstat(fd)
    if fst.st_nlink != 1:
        unexpected_error("file '%s' has %d hard links." % \
                (filename, fst.st_nlink))
    if stat.S_ISDIR(fst.st_mode):
        unexpected_error("file '%s' is a directory." % filename)
    for i in stat.ST_DEV, stat.ST_INO, stat.ST_UID, stat.ST_GID, stat.ST_MODE, stat.ST_NLINK:
        if fst[i] != lst[i]:
            unexpected_error("file status changed unexpectedly")
    return fd

def safe_open(filename):
    """Safely open a file, creating it if it doesn't exist, and return a
    r/w file descriptor."""
    # This borrows from postfix code.
    vprint("Opening archive...")
    try:
        fd = safe_open_existing(filename)
    except OSError, e:
        if e.errno != errno.ENOENT: raise
        fd = safe_open_create(filename)
    return fd

# this is where it all happens, folks
if __name__ == '__main__':
    main()
