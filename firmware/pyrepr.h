// SPDX-License-Identifier: Apache-2.0
//
// Python's str()/repr() of the values a JSON file holds, as the board's validator prints
// them in its messages (tt_harness/demo/validate.py formats values with f-strings):
//
// - an integer literal prints as Python's int: "-0" is 0;
// - a number with a fraction or an exponent is a Python float: repr's shortest digits that
//   read back as the same IEEE double (`1.0`, `1e+22`, `2.5e-07`, `inf`), computed exactly
//   with fixed-size big integers, no floating point (VexRiscv has no FPU);
// - NaN, Infinity, -Infinity (which Python's JSON reader accepts) are nan, inf, -inf;
// - one character of a string inside repr's quotes.
//
// Limits: a number with more than PY_MAX_DIGITS significant digits is not handled (the
// parser refuses it as a firmware limit first). For characters outside ASCII,
// py_isprintable knows the non-printable ranges listed in pyrepr.c (control, format,
// separator, surrogate, private use, noncharacters and the large unassigned planes); a
// code point unassigned in Python's Unicode database outside them prints as itself where
// Python would escape it.

#ifndef TT_FIRMWARE_PYREPR_H
#define TT_FIRMWARE_PYREPR_H

#include <stddef.h>
#include <stdint.h>

#include "text.h"

#include "firmware_limits.h"  // PY_MAX_DIGITS

// The number of significant digits of a JSON number literal (leading and trailing zeros
// not counted; 0 for zero).
size_t py_significant_digits(const char *lit, size_t n);
// str(int(lit)) of a JSON integer literal.
void py_int(struct text *t, const char *lit, size_t n);
// str(float(lit)) of a JSON number literal, or of NaN, Infinity, -Infinity.
void py_float(struct text *t, const char *lit, size_t n);
// str.isprintable() of one code point.
int py_isprintable(uint32_t cp);
// One code point as UTF-8 (a surrogate as its three-byte form).
void py_utf8(struct text *t, uint32_t cp);
// One code point as repr() writes it inside a string quoted with `quote` (' or ").
void py_repr_char(struct text *t, uint32_t cp, char quote);

#endif  // TT_FIRMWARE_PYREPR_H
