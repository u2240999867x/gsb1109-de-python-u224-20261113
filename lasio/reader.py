import codecs
import io
import logging
import os
import re
import sys
import traceback
import urllib.request
from io import StringIO

import numpy as np

from . import defaults, exceptions
from .las_items import CurveItem, HeaderItem, OrderedDict, SectionItems

logger = logging.getLogger(__name__)

URL_REGEXP = re.compile(
    r"^(?:http|ftp)s?://"  # http:// or https://
    r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}"
    r"\.?|[A-Z0-9-]{2,}\.?)|"  # (cont.) domain...
    r"localhost|"  # localhost...
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # ...or ip
    r"(?::\d+)?"  # optional port
    r"(?:/?|[/?]\S+)$",
    re.IGNORECASE,
)

# sow (Split On Whitespace) regex
sow_regex = re.compile(r"""([^\s"']+)|"([^"]*)"|'([^']*)'""")


def define_line_splitter(provisional_delimiter):
    """Define multiple line splitters

    return the one that is right for the data delmiter

    """
    # Split on whitespace
    # Split into non-space strings and strings within either double or single
    # quotes
    sow_regex = re.compile(r"""([^\s"']+)|"([^"]*)"|'([^']*)'""")
    # Split on tabs
    # Split into non-tab strings and strings within either double or single
    # quotes
    sot_regex = re.compile(r"""([^\t"']+)|"([^"]*)"|'([^']*)'""")

    def split_on_whitespace(line):
        return sow_regex.findall(line)

    def split_on_tabs(line):
        return sot_regex.findall(line)

    def split_on_comma(line):
        return line.split(",")

    splitters = {
        "SPACE": split_on_whitespace,
        "COMMA": split_on_comma,
        "TAB": split_on_tabs,
    }

    return splitters[provisional_delimiter]


def check_for_path_obj(file_ref):
    """Check if file_ref is a pathlib.Path object.

    If file_ref is a pathlib.Path object, then return its absolute file
    path as a string so it will get processed as other string filenames.

    If pathlib is not available, do nothing and return file_ref.

    """
    try:
        from pathlib import Path
    except ImportError:
        return file_ref

    if isinstance(file_ref, Path):
        return file_ref.absolute().__str__()
    else:
        return file_ref


def open_file(file_ref, **encoding_kwargs):
    """Open a file if necessary.

    If ``autodetect_encoding=True`` then  ``chardet`` needs to be installed, or
    else an ``ImportError`` will be raised.

    Arguments:
        file_ref (file-like object, str): either a filename, an open file
            object, or a string containing the contents of a file.

    See :func:`lasio.reader.open_with_codecs` for keyword arguments that can be
    used here.

    Returns:
        tuple of an open file-like object, and the encoding that
        was used to decode it (if it were read from disk).

    """

    file_ref = check_for_path_obj(file_ref)

    encoding = None
    if isinstance(file_ref, str):  # file_ref != file-like object, so what is it?
        lines = file_ref.splitlines()
        first_line = lines[0]
        if URL_REGEXP.match(first_line):  # it's a URL
            logger.info("Loading URL {}".format(first_line))

            response = urllib.request.urlopen(file_ref)
            if response.headers.get_content_charset() is None:
                if "encoding" in encoding_kwargs:
                    encoding = encoding_kwargs["encoding"]
                else:
                    encoding = "utf-8"
            else:
                encoding = response.headers.get_content_charset()
            # newline=None causes StringIO to use universal-newline:
            # Lines in the input can end in '\n', '\r', or '\r\n', and these are
            # translated into '\n' before being returned to the caller.
            file_ref = StringIO(response.read().decode(encoding), newline=None)
            logger.debug("Retrieved data decoded via {}".format(encoding))
        elif len(lines) > 1:  # it's LAS data as a string.
            file_ref = StringIO(file_ref)
        else:  # it must be a filename
            file_ref, encoding = open_with_codecs(first_line, **encoding_kwargs)
    return file_ref, encoding


def open_with_codecs(
    filename,
    encoding=None,
    encoding_errors="replace",
    autodetect_encoding=True,
    autodetect_encoding_chars=4000,
):
    """
    Read Unicode data from file.

    Arguments:
        filename (str): path to file

    Keyword Arguments:
        encoding (str): character encoding to open file_ref with, using
            :func:`io.open`.
        encoding_errors (str): 'strict', 'replace' (default), 'ignore' - how to
            handle errors with encodings (see
            `this section
            <https://docs.python.org/3/library/codecs.html#codec-base-classes>`__
            of the standard library's :mod:`codecs` module for more information)
        autodetect_encoding (str or bool): default True to use
            `chardet <https://github.com/chardet/chardet>`__ to detect
            encoding.
            Note if set to False several common encodings will be tried but
            chardet won't be used.
        autodetect_encoding_chars (int/None): number of chars to read from LAS
            file for auto-detection of encoding.

    Returns:
        a unicode or string object

    This function is called by :func:`lasio.reader.open_file`.

    """
    if autodetect_encoding_chars:
        nbytes = int(autodetect_encoding_chars)
    else:
        nbytes = None

    # Forget [c]chardet - if we can locate the BOM we just assume that's correct.
    nbytes_test = min(32, os.path.getsize(filename))
    with open(filename, mode="rb") as test:
        raw = test.read(nbytes_test)
    if raw.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
        autodetect_encoding = False

    # If BOM wasn't found...
    if (autodetect_encoding) and (not encoding):
        with open(filename, mode="rb") as test:
            if nbytes is None:
                raw = test.read()
            else:
                raw = test.read(nbytes)
        encoding = get_encoding(autodetect_encoding, raw)
        autodetect_encoding = False

    # Or if no BOM found & chardet not installed
    if (not autodetect_encoding) and (not encoding):
        encoding = adhoc_test_encoding(filename)
        if encoding:
            logger.info(
                "{} was found by ad hoc to work but note it might not"
                " be the correct encoding".format(encoding)
            )

    # Now open and return the file-like object
    logger.info(
        'Opening {} as {} and treating errors with "{}"'.format(
            filename, encoding, encoding_errors
        )
    )
    file_obj = io.open(filename, mode="r", encoding=encoding, errors=encoding_errors)
    return file_obj, encoding


def adhoc_test_encoding(filename):
    test_encodings = ["ascii", "windows-1252", "latin-1"]
    for i in test_encodings:
        encoding = i
        with io.open(filename, mode="r", encoding=encoding) as f:
            try:
                f.readline()
                break
            except UnicodeDecodeError:
                logger.debug("{} tested, raised UnicodeDecodeError".format(i))
                pass
            encoding = None
    return encoding


def get_encoding(auto, raw):
    """
    Automatically detect character encoding.

    Arguments:
        auto (str): auto-detection of character encoding - can be one of
            'chardet', False, or True (the latter will pick the fastest
            available option)
        raw (bytes): array of bytes to detect from

    Returns:
        A string specifying the character encoding.

    """
    if auto is True:
        try:
            import chardet
        except ImportError:
            logger.debug(
                "chardet is recommended for automatic detection of character"
                "encodings. Instead trying some common encodings."
            )
            return None
        else:
            logger.debug("get_encoding Using chardet")
            method = "chardet"
    elif auto.lower() == "chardet":
        import chardet

        logger.debug("get_encoding Using chardet")
        method = "chardet"
    result = chardet.detect(raw)
    logger.debug(
        "{} method detected encoding of {} at confidence {}".format(
            method, result["encoding"], result["confidence"]
        )
    )
    return result["encoding"]


def find_sections_in_file(file_obj):
    """Find LAS sections in a file.

    Arguments:
        file_obj: file-like object open for reading at the beginning of the section

    Returns: a list of lists *(k, first_line_no, last_line_no, line]*.
        *file_pos* is the position in the *file_obj* in bytes,
        *first_line_no* is the first line number of the section (starting
        from zero), and *line* is the contents of the section title/definition
        i.e. beginning with ``~`` but stripped of beginning or ending whitespace
        or line breaks.

    """
    file_pos = int(file_obj.tell())
    starts = []
    ends = []
    line_no = 0
    line = file_obj.readline()
    # for i, line in enumerate(file_obj):
    while line:
        sline = line.strip().strip("\n")
        if sline.startswith("~"):
            starts.append((file_pos, line_no, sline))
            if len(starts) > 1:
                ends.append(line_no - 1)
        file_pos = int(file_obj.tell())
        line = file_obj.readline()
        line_no = line_no + 1

    ends.append(line_no)
    section_positions = []
    for j, (file_pos, first_line_no, sline) in enumerate(starts):
        section_positions.append((file_pos, first_line_no, ends[j], sline))
    return section_positions


def determine_section_type(section_title):
    """Return the type of the LAS section based on its title

        >>> determine_section_type("~Curves Section")
        "Header"
        >>> determine_section_type("~ASCII")
        "Data"

    Returns: str

    """
    stitle = section_title.strip().strip("\n")
    # '~Log_Data' is a LAS-3.0 equivalent for the ~ASCII data section
    if stitle[:2] == "~A" or "~Log_Data" in stitle:
        return "Data"
    elif stitle[:2] == "~O":
        return "Header (other)"
    # This is las3 transitional code till data parsing is robust for ~A and
    # '_Data' sections
    elif re.search("_data", stitle.lower()):
        return "Las3_Data"
    else:
        return "Header items"


def inspect_data_section(file_obj, line_nos, regexp_subs, ignore_data_comments="#"):
    """Determine how many columns there are in the data section.

    Arguments:
        file_obj: file-like object open for reading at the beginning of the section
        line_nos (tuple): the first and last line no of the section to read
        regexp_subs (list): each item should be a tuple of the pattern and
            substitution string for a call to re.sub() on each line of the
            data section. See defaults.py READ_SUBS and NULL_SUBS for examples.
        ignore_data_comments (str): lines beginning with this character will be ignored

    Returns:
        n_cols, regexp_subs: integer number of columns or -1 where they are different,
        and the recommended set of regexp_subs (removing hyphen-replacing substitutions
        when we find a hyphen in every line)

    """

    line_no = line_nos[0]
    title_line = file_obj.readline()

    item_counts = []
    hyphen_exists = []

    for i, line in enumerate(file_obj):
        line_no = line_no + 1
        line = line.strip("\n").strip()
        if "-" in line:
            hyphen_exists.append(i)
        if line.strip().startswith(ignore_data_comments):
            continue
        else:
            for pattern, sub_str in regexp_subs:
                line = re.sub(pattern, sub_str, line)
            # split line and count number of elements
            n_items = len(["".join(t) for t in sow_regex.findall(line)])
            logger.trace_lasio(
                "Line {}: {} items counted in '{}'".format(line_no + 1, n_items, line)
            )
            item_counts.append(n_items)
            if (line_no == line_nos[1]) or (i >= 20):
                break

    if len(hyphen_exists) == len(item_counts):
        logger.debug(
            f"Found a hyphen in every line of the sample data section ({len(item_counts)} lines)"
        )
        hyphen_sub_keys = defaults.HYPHEN_SUBS
        hyphen_subs = []
        for key in hyphen_sub_keys:
            for sub in defaults.READ_SUBS[key]:
                hyphen_subs.append(sub)
        logger.trace_lasio(f"Removing {hyphen_subs}")
        regexp_subs = [s for s in regexp_subs if s not in hyphen_subs]
        logger.debug(
            f"Removed {hyphen_sub_keys} if present; recommending instead: {regexp_subs}"
        )

    try:
        assert len(set(item_counts)) == 1
    except AssertionError:
        logger.debug("Inconsistent number of columns {}".format(item_counts))
        return -1, regexp_subs
    else:
        logger.debug("Consistently found {} columns".format(item_counts[0]))
        return item_counts[0], regexp_subs


def read_data_section_iterative_normal_engine(
    file_obj,
    line_nos,
    regexp_subs,
    value_null_subs,
    ignore_data_comments,
    n_columns,
    dtypes,
    line_splitter,
):
    """Read data section into memory.

    Arguments:
        file_obj: file-like object open for reading at the beginning of the section
        line_nos (tuple): the first and last line no of the section to read
        regexp_subs (list): each item should be a tuple of the pattern and
            substitution string for a call to re.sub() on each line of the
            data section. See defaults.py READ_SUBS and NULL_SUBS for examples.
        value_null_subs (list): list of numerical values to be replaced by
            numpy.nan values.
        ignore_data_comments (str): lines beginning with this character will be ignored
        n_columns (int): expected number of columns
        dtypes (list, "auto", False): list of expected data types for each column,
            (each data type can be specified as e.g. `int`,
            `float`, `str`, `datetime`). If you specify 'auto', then this function
            will attempt to convert each column to a float and if that fails,
            the column will be returned as a string. If you specify False, no
            conversion of data types will be attempt at all.
        line_splitter (function): This function is dynamically configured to
            split data lines on the configured delimiter

    Returns: generator which yields the data as a 1D ndarray for each column at a time.

    """
    logger.debug(
        "Attempting to read {} columns between lines {}".format(n_columns, line_nos)
    )

    title = file_obj.readline()

    def items(f, start_line_no, end_line_no):
        for line_no, line in enumerate(f, start=start_line_no+1):
            line = line.strip("\n").strip()
            if line.startswith(ignore_data_comments):
                continue
            else:
                for pattern, sub_str in regexp_subs:
                    line = re.sub(pattern, sub_str, line)
                line = line.replace(chr(26), "")
                if len(line) == 0:
                    continue

                # for item in split_on_whitespace(line, sow_regex):
                # for item in ["".join(t) for t in sow_regex.findall(line)]:
                for item in ["".join(t) for t in line_splitter(line)]:
                    try:
                        yield np.float64(item)
                    except ValueError:
                        yield item
                if line_no == end_line_no:
                    break

    logger.debug("Reading complete data section...")
    array = np.array(
        [i for i in items(file_obj, start_line_no=line_nos[0], end_line_no=line_nos[1])]
    )
    for value in value_null_subs:
        array[array == value] = np.nan

    logger.debug("Read {} items in data section".format(len(array)))

    # Cater for situations where the data section is empty.
    if len(array) == 0:
        logger.warning("Data section is empty therefore setting n_columns to zero")
        n_columns = 0

    # Re-shape the 1D array to a 2D array.
    if n_columns > 0:
        logger.debug("Attempt re-shape to {} columns".format(n_columns))
        try:
            array = np.reshape(array, (-1, n_columns))
        except ValueError as exception:
            error_message = "Cannot reshape ~A data size {0} into {1} columns".format(
                array.shape, n_columns
            )
            if sys.version_info.major < 3:
                exception.message = error_message
                raise exception
            else:
                raise ValueError(error_message).with_traceback(exception.__traceback__)

    # Identify how many columns have actually been found.
    if len(array.shape) < 2:
        arr_n_cols = 0
    else:
        arr_n_cols = array.shape[1]

    # Identify what the appropriate data types should be for each column based on the first
    # row of the data.
    if dtypes == "auto":
        if len(array) > 0:
            dtypes = identify_dtypes_from_data(array[0, :])
        else:
            dtypes = []
    elif dtypes is False:
        dtypes = [str for n in range(arr_n_cols)]

    # Iterate over each column, convert to the appropriate dtype (if possible)
    # and then yield the data column.
    for col_idx in range(arr_n_cols):
        curve_arr = array[:, col_idx]
        curve_dtype = dtypes[col_idx]
        try:
            curve_arr = curve_arr.astype(curve_dtype, copy=False)
        except ValueError:
            logger.warning(
                "Could not convert curve #{} to {}".format(col_idx, curve_dtype)
            )
        else:
            logger.debug(
                "Converted curve {} to {} ({})".format(col_idx, curve_dtype, curve_arr)
            )
        yield curve_arr


class WrappedDataRepair(object):
    """Record of repairs made while reading a wrapped (``WRAP=YES``) data section.

    A wrapped depth step occupies several physical lines. If a cell is dropped
    from one depth step, every following column is shifted until a depth line
    is encountered which can be recognised as a valid re-alignment point.
    This object records every depth step affected, the raw lines it occupies,
    and how it was handled.

    Attributes:
        activated (bool): True if at least one depth step needed repair.
        frames (list of dict): one entry per affected depth step, with keys:

            * ``frame_index`` (int): zero-based position of the depth step
            * ``depth``: index value used for the depth step (NaN if it could
              not be determined)
            * ``first_line`` / ``last_line`` (int): 1-based range of raw file
              lines the depth step's tokens were read from
            * ``kind`` (str): one of ``missing_depth_anchor`` (the depth line
              was absent but all values were recovered),
              ``padded_missing_cells`` (one or more cells were missing; the
              depth step is retained as null values),
              ``inserted_gap`` (a depth step with no usable tokens, inserted
              to keep the depth sequence intact), or
              ``short_final_frame`` (the file ended mid-depth-step)
            * ``missing_cells`` (int): number of cells that could not be
              assigned to a column

    """

    def __init__(self):
        self.activated = False
        self.frames = []

    @property
    def raw_line_range(self):
        """tuple: the first and last raw file line involved in any repair."""
        if not self.frames:
            return None
        return (
            min(frame["first_line"] for frame in self.frames),
            max(frame["last_line"] for frame in self.frames),
        )

    def add_frame(
        self, frame_index, depth, first_line, last_line, kind, missing_cells=0
    ):
        self.activated = True
        self.frames.append(
            {
                "frame_index": frame_index,
                "depth": depth,
                "first_line": first_line,
                "last_line": last_line,
                "kind": kind,
                "missing_cells": missing_cells,
            }
        )

    def __len__(self):
        return len(self.frames)

    def __repr__(self):
        if not self.activated:
            return "WrappedDataRepair(no repairs needed)"
        lines = [
            "WrappedDataRepair: {} depth step(s) affected, raw lines {}-{}".format(
                len(self.frames), self.raw_line_range[0], self.raw_line_range[1]
            )
        ]
        for frame in self.frames:
            lines.append(
                "  frame {frame_index} (depth {depth}): {kind} on raw lines "
                "{first_line}-{last_line}".format(**frame)
            )
        return "\n".join(lines)


def _to_float(value):
    """Convert a header value to a finite np.float64, or return None."""
    if value is None:
        return None
    try:
        result = np.float64(value)
    except (ValueError, TypeError):
        return None
    return result if np.isfinite(result) else None


def _convert_token(item):
    """Convert one token to np.float64 if possible, otherwise leave as str."""
    try:
        return np.float64(item)
    except ValueError:
        return item


def _tokenize_wrapped_data(
    file_obj, line_nos, regexp_subs, ignore_data_comments, line_splitter
):
    """Flatten a wrapped data section into a tagged stream of tokens.

    Returns:
        tokens (list of (value, line_no) tuples): every data token in file
            order, tagged with its zero-based source line number (the rest
            of the reader adds one when quoting a physical line).
        anchor_positions (dict): maps the token stream position of each line
            that held a single numeric token to its (value, line_no). Such a
            line is a depth anchor in a properly formatted wrapped section,
            although a continuation line carrying one final value looks the
            same; candidates are validated against the depth sequence later.

    """
    title = file_obj.readline()
    tokens = []
    anchor_positions = {}
    for line_no, line in enumerate(file_obj, start=line_nos[0] + 1):
        line = line.strip("\n").strip()
        if line.startswith(ignore_data_comments):
            continue
        for pattern, sub_str in regexp_subs:
            line = re.sub(pattern, sub_str, line)
        line = line.replace(chr(26), "")
        if len(line) == 0:
            continue
        items = [_convert_token("".join(t)) for t in line_splitter(line)]
        start_position = len(tokens)
        tokens.extend((item, line_no) for item in items)
        if len(items) == 1 and isinstance(items[0], np.float64):
            anchor_positions[start_position] = (items[0], line_no)
        if line_no == line_nos[1]:
            break
    return tokens, anchor_positions


def read_wrapped_data_section(
    file_obj,
    line_nos,
    regexp_subs,
    value_null_subs,
    ignore_data_comments,
    n_columns,
    dtypes,
    line_splitter,
    step=None,
    step_tolerance=None,
    repair=False,
    repair_report=None,
):
    """Read a wrapped (``WRAP=YES``) data section, optionally repairing it.

    Depth steps are rebuilt as frames using the declared number of columns
    (``n_columns``), the depth direction and step from the header, a tolerance
    for matching depth anchors, and the depth anchor lines themselves as
    re-alignment points.

    Arguments:
        file_obj: file-like object open at the start of the data section.
        line_nos (tuple): first and last line number of the section.
        regexp_subs (list): regex substitutions applied to each line.
        value_null_subs (list): numerical values replaced by numpy.nan.
        ignore_data_comments (str): comment prefix for data lines.
        n_columns (int): declared number of columns (from the ~C section).
        dtypes (list, "auto", False): per-column data type specification.
        line_splitter (function): line tokenizer for the configured delimiter.
        step (float or None): the declared STEP value; its sign gives the
            depth direction. If None, the step is inferred from the first two
            depth anchors.
        step_tolerance (float or None): maximum absolute difference accepted
            when matching a depth anchor against the expected depth sequence.
            If None, a tolerance of one thousandth of the step is used.
        repair (bool): if False, any frame that cannot be assembled raises
            :class:`lasio.exceptions.WrappedDataError`. If True, the damaged
            depth step is retained as null values and parsing re-aligns at the
            next valid depth anchor.
        repair_report (WrappedDataRepair or None): object which, if supplied,
            is filled with a record of every repair made.

    Returns:
        generator yielding a 1D ndarray per column, in the same manner as
        :func:`read_data_section_iterative_normal_engine`.

    """
    logger.debug(
        "Reading wrapped data section: {} columns, repair={}".format(
            n_columns, repair
        )
    )

    if repair_report is None:
        repair_report = WrappedDataRepair()

    tokens, anchor_positions = _tokenize_wrapped_data(
        file_obj, line_nos, regexp_subs, ignore_data_comments, line_splitter
    )

    if len(tokens) == 0:
        logger.warning("Data section is empty therefore setting n_columns to zero")
        n_columns = 0
        array = np.empty((0, 0))
    elif n_columns <= 0:
        raise exceptions.WrappedDataError(
            "Cannot read a wrapped data section without a declared column count"
        )
    else:
        flat_array = np.array([value for value, _ in tokens], dtype=object)
        # Healthy wrapped sections flatten to an exact multiple of the
        # declared number of columns: keep the historical reshape behaviour
        # for them so parsing is unchanged. A count mismatch (a dropped cell
        # or depth line), or depth anchors which no longer follow the STEP
        # sequence (a whole depth step removed), triggers the frame-aware
        # repair machinery instead.
        if flat_array.size % n_columns == 0 and _anchors_match_sequence(
            anchor_positions, n_columns, _to_float(step), _to_float(step_tolerance)
        ):
            array = np.reshape(flat_array, (-1, n_columns))
        else:
            array = _assemble_wrapped_frames(
                tokens=tokens,
                anchor_positions=anchor_positions,
                n_columns=n_columns,
                declared_step=_to_float(step),
                step_tolerance=_to_float(step_tolerance),
                repair=repair,
                repair_report=repair_report,
            )

    for value in value_null_subs:
        array[array == value] = np.nan

    if len(array.shape) < 2:
        arr_n_cols = 0
    else:
        arr_n_cols = array.shape[1]

    if dtypes == "auto":
        if len(array) > 0:
            dtypes = identify_dtypes_from_data(array[0, :])
        else:
            dtypes = []
    elif dtypes is False:
        dtypes = [str for n in range(arr_n_cols)]

    for col_idx in range(arr_n_cols):
        curve_arr = array[:, col_idx]
        curve_dtype = dtypes[col_idx]
        try:
            curve_arr = curve_arr.astype(curve_dtype, copy=False)
        except ValueError:
            logger.warning(
                "Could not convert curve #{} to {}".format(col_idx, curve_dtype)
            )
        yield curve_arr


def _anchors_match_sequence(
    anchor_positions, n_columns, declared_step, step_tolerance
):
    """Check depth anchors on frame boundaries follow one-step increments.

    A whole depth step can be deleted without changing the divisibility of
    the token count; in that case two consecutive depth anchors jump by two
    (or more) steps. Pairwise differences are compared rather than an
    arithmetic sequence, so ordinary depth drift does not trigger a false
    alarm. Returns False as soon as such a jump is detected.
    """
    boundary_anchors = sorted(
        position
        for position in anchor_positions
        if position % n_columns == 0
    )
    if len(boundary_anchors) < 2:
        return True
    depths = [anchor_positions[position][0] for position in boundary_anchors]
    step = declared_step
    if step is None or step == 0:
        first_difference = depths[1] - depths[0]
        step = first_difference if first_difference != 0 else None
    if step is None:
        return True
    if step_tolerance is None:
        step_tolerance = abs(step) * 1e-3
    for previous, current in zip(depths, depths[1:]):
        if abs((current - previous) - step) > step_tolerance:
            return False
    return True


def _resolve_step(declared_step, first_depth, second_depth):
    """Return a finite, non-zero step or None."""
    if declared_step is not None and declared_step != 0:
        return np.float64(declared_step)
    if first_depth is not None and second_depth is not None:
        inferred = second_depth - first_depth
        if inferred != 0:
            return inferred
    return None


def _depth_matches(observed, expected, tolerance):
    if expected is None or tolerance is None:
        return True
    try:
        return abs(observed - expected) <= tolerance
    except TypeError:
        return False


def _assemble_wrapped_frames(
    tokens,
    anchor_positions,
    n_columns,
    declared_step,
    step_tolerance,
    repair,
    repair_report,
):
    """Rebuild depth-step frames from the tagged token stream."""
    n_values = n_columns - 1
    frames = []

    position = 0
    frame_index = 0
    first_depth = None
    step = declared_step
    tolerance = step_tolerance

    def expected_depth(index):
        if first_depth is None or step is None:
            return None
        return first_depth + index * step

    def record_null_frame(depth, line_span, kind, missing_cells=0):
        if line_span is not None:
            # Internal line numbers are zero-based; the report uses physical
            # (1-based) file lines.
            first_line, last_line = line_span[0] + 1, line_span[1] + 1
        else:
            first_line, last_line = None, None
        frames.append(
            [depth if depth is not None else np.nan] + [np.nan] * n_values
        )
        repair_report.add_frame(
            frame_index=len(frames) - 1,
            depth=frames[-1][0],
            first_line=first_line,
            last_line=last_line,
            kind=kind,
            missing_cells=missing_cells,
        )

    while position < len(tokens):
        expected = expected_depth(frame_index)

        anchor = anchor_positions.get(position)
        anchor_is_valid = anchor is not None and (
            first_depth is None or _depth_matches(anchor[0], expected, tolerance)
        )
        if anchor_is_valid:
            depth = anchor[0]
            position += 1
            if first_depth is None:
                first_depth = depth
                second_anchor = next(
                    (
                        candidate[0]
                        for candidate_position, candidate in anchor_positions.items()
                        if candidate_position > position
                    ),
                    None,
                )
                step = _resolve_step(step, first_depth, second_anchor)
                if step is not None and tolerance is None:
                    tolerance = abs(step) * 1e-3
        else:
            # No valid depth anchor line at the start of this depth step.
            depth = expected

        values = []
        value_lines = []
        while len(values) < n_values and position < len(tokens):
            # A single-token line is only a frame boundary when its value
            # matches the next frame's expected depth; otherwise it is an
            # ordinary value token (e.g. the final value of a frame).
            if position in anchor_positions and _depth_matches(
                anchor_positions[position][0],
                expected_depth(frame_index + 1),
                tolerance,
            ):
                break
            value, line_no = tokens[position]
            values.append(value)
            value_lines.append(line_no)
            position += 1

        if len(values) == n_values:
            if not anchor_is_valid:
                if not repair:
                    raise exceptions.WrappedDataError(
                        "Wrapped data section depth step {} (raw line {}) has no "
                        "depth anchor line. Read with repair_wrapped_data=True to "
                        "rebuild it from the STEP-derived depth sequence.".format(
                            frame_index, min(value_lines) + 1
                        )
                    )
                # Corroborate the frame: the next depth anchor must land
                # exactly one frame on. Otherwise the anchors themselves are
                # unrecognisable (e.g. tolerance too tight) and accepting the
                # frame would be a guess.
                corroboration = _find_realignment(
                    anchor_positions=anchor_positions,
                    from_position=position,
                    first_depth=first_depth,
                    step=step,
                    tolerance=tolerance,
                    min_frame_index=frame_index + 1,
                )
                if (
                    corroboration is None
                    or corroboration[2] != frame_index + 1
                ):
                    raise exceptions.WrappedDataError(
                        "Depth step {} (raw line {}) has no depth anchor and "
                        "the next anchor cannot be matched to the expected "
                        "depth sequence within the step tolerance; no "
                        "alignment was guessed.".format(
                            frame_index, min(value_lines) + 1
                        )
                    )
            frames.append(
                [depth if depth is not None else np.nan] + values
            )
            if not anchor_is_valid:
                repair_report.add_frame(
                    frame_index=len(frames) - 1,
                    depth=frames[-1][0],
                    first_line=min(value_lines) + 1,
                    last_line=max(value_lines) + 1,
                    kind="missing_depth_anchor",
                )
            frame_index += 1
            continue

        # The frame is short: at least one cell is missing.
        if value_lines:
            problem_line = min(value_lines) + 1
        elif position < len(tokens):
            problem_line = tokens[position][1] + 1
        else:
            # The section ended immediately after a depth anchor.
            problem_line = anchor[1] + 1 if anchor is not None else None
        if not repair:
            raise exceptions.WrappedDataError(
                "Wrapped data section is misaligned at depth step {} "
                "(raw line {}): expected {} value(s) but found {}. Read with "
                "repair_wrapped_data=True to retain the damaged depth step "
                "and re-align at the next valid depth anchor.".format(
                    frame_index, problem_line, n_values, len(values)
                )
            )

        realignment = _find_realignment(
            anchor_positions=anchor_positions,
            from_position=position,
            first_depth=first_depth,
            step=step,
            tolerance=tolerance,
            min_frame_index=frame_index,
        )

        if realignment is None:
            if position >= len(tokens):
                if value_lines:
                    span = (min(value_lines), max(value_lines))
                elif anchor is not None:
                    span = (anchor[1], anchor[1])
                else:
                    span = None
                record_null_frame(
                    expected,
                    span,
                    "short_final_frame",
                    missing_cells=n_values - len(values),
                )
                frame_index += 1
                break
            raise exceptions.WrappedDataError(
                "Wrapped data section is misaligned from raw line {} onwards "
                "and no depth anchor re-alignment point could be found within "
                "the step tolerance.".format(problem_line)
            )

        anchor_position, anchor_line_no, target_frame_index = realignment

        # Raw lines truly involved: the surviving tokens and every line
        # consumed before the healthy re-alignment anchor line. A fully
        # deleted frame leaves no tokens, so point at the anchor line itself.
        affected_lines = value_lines + [
            line_no for _, line_no in tokens[position:anchor_position]
        ]
        if affected_lines:
            line_span = (min(affected_lines), max(affected_lines))
        else:
            line_span = (anchor_line_no, anchor_line_no)

        # No surviving tokens means the whole depth step was deleted rather
        # than merely having some cells dropped.
        kind = "padded_missing_cells" if values else "inserted_gap"
        record_null_frame(
            expected,
            line_span,
            kind,
            missing_cells=n_values - len(values),
        )
        frame_index += 1

        # Depth steps skipped entirely between the damage and the re-alignment.
        while frame_index < target_frame_index:
            record_null_frame(
                expected_depth(frame_index),
                None,
                "inserted_gap",
                missing_cells=n_values,
            )
            frame_index += 1

        position = anchor_position

    if not frames:
        return np.empty((0, n_columns))
    return np.array(frames, dtype=object)


def _find_realignment(
    anchor_positions, from_position, first_depth, step, tolerance, min_frame_index
):
    """Find the first depth anchor matching one expected depth sequence point.

    Returns a (token_position, line_no, frame_index) tuple or None. If an
    anchor matches more than one expected frame index within the tolerance
    the alignment is ambiguous, so None is returned rather than guessing.
    """
    if first_depth is None or step is None or tolerance is None:
        return None
    for token_position in sorted(anchor_positions):
        if token_position < from_position:
            continue
        depth, line_no = anchor_positions[token_position]
        exact_index = (depth - first_depth) / step
        candidate_index = int(round(float(exact_index)))
        if candidate_index < min_frame_index:
            continue
        matches = [
            index
            for index in (candidate_index - 1, candidate_index, candidate_index + 1)
            if abs(depth - (first_depth + index * step)) <= tolerance
        ]
        if len(matches) == 1 and matches[0] == candidate_index:
            return token_position, line_no, candidate_index
    return None


def identify_dtypes_from_data(row):
    """Identify which columns should be 'str' and which 'float'.

    Args:
        row (1D ndarray): first row of data section

    Returns: list of [float, float, str, ...] etc

    """
    logger.debug("Creating auto dtype spec from first line of data array")
    dtypes_list = []
    for i, value in enumerate(row):
        try:
            value_converted = float(value)
        except:
            dtypes_list.append(str)
        else:
            dtypes_list.append(float)
        logger.debug(
            "Column {}: value {} -> dtype {}".format(i, value, dtypes_list[-1])
        )
    return dtypes_list


def read_data_section_iterative_numpy_engine(file_obj, line_nos):
    """Read data section into memory.

    Arguments:
        file_obj: file-like object open for reading at the beginning of the section
        line_nos (tuple): the first and last line no of the section to read


    Returns:
        A numpy ndarray.
    """

    first_line = line_nos[0] + 1
    last_line = line_nos[1]
    max_rows = last_line - first_line

    file_obj.seek(0)

    # unpack=True transforms the data from an array of rows to an array of columns.
    # loose=False will throw an error on non-numerical data, which then sends the 
    # parsing to the 'normal' parser.
    array = np.genfromtxt(
        file_obj, skip_header=first_line, max_rows=max_rows, names=None, unpack=True, loose=False
    )

    # If there is only one data row or a single column, np.genfromtxt treats it as one array of
    # individual values. Lasio needs an array of arrays. This if statement
    # converts the single line data array or a single column to an array of arrays(column data).
    if len(array.shape) == 1:
        arr_len = array.shape[0]
        if max_rows == 1:
            array = array.reshape(arr_len, 1)
        else:
            array = array.reshape(1, arr_len)
    return array


def get_substitutions(read_policy, null_policy):
    """Parse read and null policy definitions into a list of regexp and value
    substitutions.

    Arguments:
        read_policy (str, list, or substitution): either (1) a string defined in
            defaults.READ_POLICIES; (2) a list of substitutions as defined by
            the keys of defaults.READ_SUBS; or (3) a list of actual substitutions
            similar to the values of defaults.READ_SUBS. You can mix (2) and (3)
            together if you want.
        null_policy (str, list, or sub): as for read_policy but for
            defaults.NULL_POLICIES and defaults.NULL_SUBS

    Returns:
        regexp_subs, value_null_subs, version_NULL - two lists and a bool.
        The first list is pairs of regexp patterns and substrs, and the second
        list is just a list of floats or integers. The bool is whether or not
        'NULL' was located as a substitution.

    The default READ_POLICIES are

    * comma-decimal-mark : in numbers replace a comma divider with a decimal
    * run-on(-) : separate 2 numbers that run together on the negative sign
    * run-on(.) : replace numbers with 2 or more decimals or a NaN and a decimal with 2 NaNs


    """
    regexp_subs = []
    numerical_subs = []
    version_NULL = False

    for policy_typ, policy, policy_subs, subs in (
        ("read", read_policy, defaults.READ_POLICIES, defaults.READ_SUBS),
        ("null", null_policy, defaults.NULL_POLICIES, defaults.NULL_SUBS),
    ):
        try:
            is_policy = policy in policy_subs
        except TypeError:
            is_policy = False
        if is_policy:
            logger.debug('using {} policy of "{}"'.format(policy_typ, policy))
            all_subs = []
            for sub in policy_subs[policy]:
                logger.debug("adding substitution {}".format(sub))
                if sub in subs:
                    all_subs += subs[sub]
                if sub == "NULL":
                    logger.debug("located substitution for LAS.version.NULL as True")
                    version_NULL = True
        else:
            all_subs = []
            for item in policy:
                if item in subs:
                    all_subs += subs[item]
                    if item == "NULL":
                        logger.debug(
                            "located substitution for LAS.version.NULL as True"
                        )
                        version_NULL = True
                else:
                    all_subs.append(item)
        for item in all_subs:
            try:
                iter(item)
            except TypeError:
                logger.debug("added numerical substitution: {}".format(item))
                numerical_subs.append(item)
            else:
                logger.debug(
                    'added regexp substitution: pattern={} substr="{}"'.format(
                        item[0], item[1]
                    )
                )
                regexp_subs.append(item)
    numerical_subs = [n for n in numerical_subs if not n is None]

    return regexp_subs, numerical_subs, version_NULL


def parse_header_items_section(
    file_obj,
    line_nos,
    version,
    ignore_header_errors=False,
    mnemonic_case="preserve",
    ignore_comments=("#",),
):
    """Parse a header section dict into a SectionItems containing HeaderItems.

    Arguments:
        file_obj: file-like object open for reading at the beginning of the section
        line_nos (tuple): the first and last line no of the section to read
        version (float): either 1.2 or 2.0

    Keyword Arguments:
        ignore_header_errors (bool): if True, issue HeaderItem parse errors
            as :func:`logging.warning` calls instead of a
            :exc:`lasio.exceptions.LASHeaderError` exception.
        mnemonic_case (str): 'preserve': keep the case of HeaderItem mnemonics
                             'upper': convert all HeaderItem mnemonics to uppercase
                             'lower': convert all HeaderItem mnemonics to lowercase
        ignore_comments (list): ignore lines starting with these characters; by
            default '#'.

    Returns:
        :class:`lasio.SectionItems`

    """
    line_no = line_nos[0]
    title = file_obj.readline()
    title = title.strip("\n").strip()
    logger.debug("Line {}: Section title parsed as '{}'".format(line_no + 1, title))

    parser = SectionParser(title, version=version)

    section = SectionItems()
    assert mnemonic_case in ("upper", "lower", "preserve")
    if not mnemonic_case == "preserve":
        section.mnemonic_transforms = True

    for i, line in enumerate(file_obj):
        line_no = line_no + 1
        line = line.strip("\n").strip()
        if not line:
            logger.debug("Line {}: empty, ignoring".format(line_no + 1))
        elif line[0] in ignore_comments:
            logger.debug(
                "Line {}: treating as a comment and ignoring: '{}'".format(
                    line_no + 1, line
                )
            )
        else:
            # We have arrived at a new section so break and return the previous
            # section's object.
            if line.startswith("~"):
                break
            try:
                values = read_line(line, section_name=parser.section_name2)
            except:
                message = 'Line {} (section {}): "{}"'.format(line_no + 1, title, line)
                if ignore_header_errors:
                    logger.warning(message)
                else:
                    raise exceptions.LASHeaderError(message)
            else:
                if mnemonic_case == "upper":
                    values["name"] = values["name"].upper()
                elif mnemonic_case == "lower":
                    values["name"] = values["name"].lower()
                item = parser(**values)
                logger.debug("Line {}: parsed as {}".format(line_no + 1, item))
                section.append(item)
        if line_no == line_nos[1]:
            break

    return section


class SectionParser(object):

    """Parse lines from header sections.

    Arguments:
        title (str): title line of section. Used to understand different
            order formatting across the special sections ~C, ~P, ~W, and ~V,
            depending on version 1.2 or 2.0.

    Keyword Arguments:
        version (float): version to parse according to. Default is 1.2.

    """

    def __init__(self, title, version=1.2):
        las3_section_indicators = ["_DATA", "_PARAMETER", "_DEFINITION"]

        is_like_las3_section = any(
            [section_str in title.upper() for section_str in las3_section_indicators]
        )

        # On the first call to SectionParser ~Version hasn't been parsed.  So
        # the version number will report the default. Although the ~Version
        # section is supposed to be the first section, there can be las files
        # in the wild that don't have the ~Version or doesn't have it first. In
        # those cases a Las3 file would end up parsed as a Las2 file or
        # partially parsed as a Las2 file.
        if version == 3.0 and is_like_las3_section:
            self.func = self.metadata
            self.section_name2 = title
            self.default_order = "value:descr"
            self.orders = {}
        elif title.upper().startswith("~C"):
            self.func = self.curves
            self.section_name2 = "Curves"
        elif title.upper().startswith("~P"):
            self.func = self.params
            self.section_name2 = "Parameter"
        elif title.upper().startswith("~W"):
            self.func = self.metadata
            self.section_name2 = "Well"
        elif title.upper().startswith("~V"):
            self.func = self.metadata
            self.section_name2 = "Version"
        else:
            logger.info("Unknown section name {}".format(title.upper()))
            self.func = self.metadata
            self.section_name2 = title
            self.default_order = "value:descr"
            self.orders = {}

        self.version = version
        self.section_name = title

        defs = defaults.ORDER_DEFINITIONS

        if self.section_name2 in defs[self.version]:
            section_orders = defs[self.version][self.section_name2]
            self.default_order = section_orders[0]  #
            self.orders = {}
            for order, mnemonics in section_orders[1:]:
                for mnemonic in mnemonics:
                    self.orders[mnemonic] = order

    def __call__(self, **keys):
        """Return the correct object for this type of section.

        Refer to :meth:`lasio.reader.SectionParser.metadata`,
        :meth:`lasio.reader.SectionParser.params`, and
        :meth:`lasio.reader.SectionParser.curves` for the methods actually
        used by this routine.

        Keyword arguments should be the key:value pairs returned by
        :func:`lasio.reader.read_header_line`.

        """
        item = self.func(**keys)
        return item

    def num(self, x, default=None):
        """Attempt to parse a number.

        Arguments:
            x (str, int, float): potential number
            default (int, float, None): fall-back option

        Returns:
            int, float, or **default** - from most to least preferred types.

        """
        if default is None:
            default = x

        # in case it is a string.
        try:
            pattern, sub = defaults.READ_SUBS["comma-decimal-mark"][0]
            x = re.sub(pattern, sub, x)
        except:
            pass

        try:
            return np.int64(x)
        except:
            try:
                x = np.float64(x)
            except:
                return default
        if np.isfinite(x):
            return x
        else:
            return default

    def strip_brackets(self, x):
        x = x.strip()
        if len(x) >= 2:
            if (x[0] == "[" and x[-1] == "]") or (x[0] == "(" and x[-1] == ")"):
                return x[1:-1]
        return x

    def metadata(self, **keys):
        """Return HeaderItem correctly formatted according to the order
        prescribed for LAS v 1.2 or 2.0 for the ~W section.

        Keyword arguments should be the key:value pairs returned by
        :func:`lasio.reader.read_header_line`.

        """
        # number_strings: fields that shouldn't be converted to numbers
        number_strings = ["API", "UWI"]

        key_order = self.orders.get(keys["name"], self.default_order)

        value = ""
        descr = ""

        if key_order == "value:descr":
            value = keys["value"]
            descr = keys["descr"]
        elif key_order == "descr:value":
            value = keys["descr"]
            descr = keys["value"]

        if keys["name"].upper() not in number_strings:
            value = self.num(value)

        item = HeaderItem(
            keys["name"],  # mnemonic
            self.strip_brackets(keys["unit"]),  # unit
            value,  # value
            descr,  # descr
        )
        return item

    def curves(self, **keys):
        """Return CurveItem.

        Keyword arguments should be the key:value pairs returned by
        :func:`lasio.reader.read_header_line`.

        """
        item = CurveItem(
            keys["name"],  # mnemonic
            self.strip_brackets(keys["unit"]),  # unit
            keys["value"],  # value
            keys["descr"],  # descr
        )
        return item

    def params(self, **keys):
        """Return HeaderItem for ~P section (the same between 1.2 and 2.0 specs)

        Keyword arguments should be the key:value pairs returned by
        :func:`lasio.reader.read_header_line`.

        """
        return HeaderItem(
            keys["name"],  # mnemonic
            self.strip_brackets(keys["unit"]),  # unit
            self.num(keys["value"]),  # value
            keys["descr"],  # descr
        )


def read_line(*args, **kwargs):
    """Retained for backwards-compatibility.

    See :func:`lasio.reader.read_header_line`.

    """
    return read_header_line(*args, **kwargs)


def read_header_line(line, pattern=None, section_name=None):
    """Read a line from a LAS header section.

    The line is parsed with a regular expression -- see LAS file specs for
    more details, but it should basically be in the format::

        name.unit       value : descr

    Arguments:
        line (str): line from a LAS header section
        section_name (str): Name of the section the 'line' is from. The default
        value is None.


    Returns:
        A dictionary with keys 'name', 'unit', 'value', and 'descr', each
        containing a string as value.

    """
    d = {"name": "", "unit": "", "value": "", "descr": ""}

    # Set defaults for local variables.
    patterns = []
    m = None

    if pattern is None:
        patterns = configure_metadata_patterns(line, section_name)
    else:  # pattern was passed in on function call
        patterns.append(pattern)

    for pattern in patterns:
        # Attempt to parse the section line's name(mnemonic), unit, value and
        # descr fields with the given pattern.
        m = re.match(pattern, line)
        if m is not None:
            break

    mdict = m.groupdict()
    for key, value in mdict.items():
        d[key] = value.strip()
        if key == "unit":
            if d[key].endswith("."):
                d[key] = d[key].strip(".")  # see issue #36
    return d


def configure_metadata_patterns(line, section_name):
    """Configure regular-expression patterns to parse section meta-data lines.

    Arguments:
        line (str): line from LAS header section
        section_name (str): Name of the section the 'line' is from.

    Returns:
        An array of regular-expression strings (patterns).
    """

    # Default return value
    patterns = []

    # Default regular expressions for name, value and desc fields
    name_re = r"\.?(?P<name>[^.]*)\."
    value_re = r"(?P<value>.*):"
    desc_re = r"(?P<descr>.*)"

    # Default regular expression for unit field. Note that we
    # attempt to match "1000 psi" as a special case which allows
    # a single whitespace character, in contradiction to the LAS specification
    # See GitHub issue #363 for details.
    unit_re = r"(?P<unit>([0-9]+\s)?[^\s]*)"

    # Alternate regular expressions for special cases
    name_missing_period_re = r"(?P<name>[^:]*):"
    value_missing_period_re = r"(?P<value>.*)"
    value_without_colon_delimiter_re = r"(?P<value>[^:]*)"
    value_with_time_colon_re = (
        r"(?P<value>.*?)(?:(?<!( [0-2][0-3]| hh| HH)):(?!([0-5][0-9]|mm|MM)))"
    )
    name_with_dots_re = r"\.?(?P<name>[^.].*[.])\."
    no_desc_re = ""
    no_unit_re = ""

    # Configure special cases
    # 1. missing period (assume that only name and value are present)
    # 2. missing colon delimiter and description field
    # 3. double_dots '..' caused by mnemonic abbreviation (with period)
    #    next to the dot delimiter.
    if ":" in line:
        if not "." in line[:line.find(":")]:
            # If there is no period, then we assume that the colon exists and
            # everything on the left is the name, and everything on the right
            # is the value - therefore no unit or description field.
            name_re = name_missing_period_re
            value_re = value_missing_period_re
            desc_re = no_desc_re
            unit_re = no_unit_re
            value_with_time_colon_re = value_missing_period_re

    if not ":" in line:
        # If there isn't a colon delimiter then there isn't
        # a description field either.
        value_re = value_without_colon_delimiter_re
        desc_re = no_desc_re

        if ".." in line and section_name == "Curves":
            name_re = name_with_dots_re
    else:
        if re.search(r"[^ ]\.\.", line) and section_name == "Curves":
            double_dot = line.find("..")
            desc_colon = line.rfind(":")

            # Check that a double_dot is not in the
            # description string.
            if double_dot < desc_colon:
                name_re = name_with_dots_re

    if section_name == "Parameter":
        # Search for a value entry with a time-value first.
        pattern = name_re + unit_re + value_with_time_colon_re + desc_re
        patterns.append(pattern)

    # Add the regular pattern for all section_names
    # for the Parameter section this will run after time-value pattern
    pattern = name_re + unit_re + value_re + desc_re
    patterns.append(pattern)

    return patterns
