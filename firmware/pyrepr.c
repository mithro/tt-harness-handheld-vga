// SPDX-License-Identifier: Apache-2.0
//
// See pyrepr.h. A float is printed in two steps, both exact:
//
// 1. The double nearest the literal (round half to even), as m * 2**e2 with m < 2**53:
//    the quotient of two big integers, digits * 10**E over 2**e2.
// 2. The shortest digits that read back as that double, the nearest of them to it (the
//    "free-format" algorithm of Steele and White as Burger and Dybvig give it, which is
//    what Python's repr computes with David Gay's dtoa mode 0), then Python's layout:
//    positional when the decimal point is at -4 < decpt <= 16, else `d.ddde+XX`.
//
// A literal with at most 15 significant digits whose value is a normal double takes a
// shortcut with no big integers: every such decimal reads back as itself (DBL_DIG is 15)
// and no shorter one reads back to the same double, so its own digits are repr's.

#include "pyrepr.h"

#include "py_printable.h"

// ---- big integers ---------------------------------------------------------------------------

#define BIG_WORDS 72  // 2304 bits; the largest needed is about 1600 (pyrepr.h limits)

struct big {
    uint32_t n;  // words in use; w[n - 1] != 0 unless n == 0
    uint32_t w[BIG_WORDS];
};

static int big_overflow;  // a result did not fit: a bug, reported as "?" in the output

static void big_trim(struct big *b) {
    while (b->n && b->w[b->n - 1] == 0) b->n--;
}

static void big_set(struct big *b, uint64_t v) {
    b->n = 0;
    while (v) {
        b->w[b->n++] = (uint32_t)v;
        v >>= 32;
    }
}

static void big_copy(struct big *d, const struct big *s) {
    d->n = s->n;
    for (uint32_t i = 0; i < s->n; i++) d->w[i] = s->w[i];
}

static void big_push(struct big *b, uint32_t w) {
    if (b->n >= BIG_WORDS) {
        big_overflow = 1;
        return;
    }
    b->w[b->n++] = w;
}

static void big_mul_add(struct big *b, uint32_t m, uint32_t add) {
    uint64_t carry = add;
    for (uint32_t i = 0; i < b->n; i++) {
        uint64_t v = (uint64_t)b->w[i] * m + carry;
        b->w[i] = (uint32_t)v;
        carry = v >> 32;
    }
    if (carry) big_push(b, (uint32_t)carry);
}

static void big_mul_pow10(struct big *b, uint32_t k) {
    while (k >= 9) {
        big_mul_add(b, 1000000000u, 0);
        k -= 9;
    }
    static const uint32_t p10[9] = {1, 10, 100, 1000, 10000, 100000, 1000000, 10000000,
                                    100000000};
    if (k) big_mul_add(b, p10[k], 0);
}

static void big_shl(struct big *b, uint32_t bits) {
    if (b->n == 0) return;
    uint32_t words = bits / 32, s = bits % 32;
    if (b->n + words + 1 > BIG_WORDS) {
        big_overflow = 1;
        return;
    }
    if (s) {
        uint32_t top = b->w[b->n - 1] >> (32 - s);
        for (uint32_t i = b->n - 1; i > 0; i--) b->w[i] = (b->w[i] << s) | (b->w[i - 1] >> (32 - s));
        b->w[0] <<= s;
        if (top) b->w[b->n++] = top;
    }
    if (words) {
        for (uint32_t i = b->n; i > 0; i--) b->w[i - 1 + words] = b->w[i - 1];
        for (uint32_t i = 0; i < words; i++) b->w[i] = 0;
        b->n += words;
    }
}

static void big_shr1(struct big *b) {
    for (uint32_t i = 0; i < b->n; i++) {
        b->w[i] >>= 1;
        if (i + 1 < b->n) b->w[i] |= b->w[i + 1] << 31;
    }
    big_trim(b);
}

static int big_cmp(const struct big *a, const struct big *b) {
    if (a->n != b->n) return a->n < b->n ? -1 : 1;
    for (uint32_t i = a->n; i > 0; i--) {
        if (a->w[i - 1] != b->w[i - 1]) return a->w[i - 1] < b->w[i - 1] ? -1 : 1;
    }
    return 0;
}

static void big_add(struct big *a, const struct big *b) {
    uint64_t carry = 0;
    uint32_t n = a->n > b->n ? a->n : b->n;
    for (uint32_t i = 0; i < n; i++) {
        uint64_t v = carry + (i < a->n ? a->w[i] : 0) + (i < b->n ? b->w[i] : 0);
        if (i < a->n) a->w[i] = (uint32_t)v;
        else big_push(a, (uint32_t)v);
        carry = v >> 32;
    }
    if (carry) big_push(a, (uint32_t)carry);
}

static void big_sub(struct big *a, const struct big *b) {  // a >= b
    int64_t borrow = 0;
    for (uint32_t i = 0; i < a->n; i++) {
        int64_t v = (int64_t)a->w[i] - (i < b->n ? b->w[i] : 0) - borrow;
        borrow = v < 0;
        a->w[i] = (uint32_t)(v + (borrow ? ((int64_t)1 << 32) : 0));
    }
    big_trim(a);
}

// a + b compared with c.
static int big_cmp_sum(const struct big *a, const struct big *b, const struct big *c) {
    static struct big s;
    big_copy(&s, a);
    big_add(&s, b);
    return big_cmp(&s, c);
}

static uint32_t bitlen64(uint64_t v) {
    uint32_t n = 0;
    while (v) {
        n++;
        v >>= 1;
    }
    return n;
}

// ---- the literal ----------------------------------------------------------------------------

struct decimal {
    int neg;
    char digits[PY_MAX_DIGITS];  // significant digits, no leading or trailing zeros
    uint32_t n;                  // 0: the value is zero
    int32_t exp;                 // value = 0.digits * 10**exp... no: digits * 10**exp
    int too_long;
};

static int is_digit(char c) { return c >= '0' && c <= '9'; }

// digits * 10**exp, from a JSON number literal (grammar already checked).
static void decimal_parse(const char *lit, size_t n, struct decimal *d) {
    size_t i = 0;
    int32_t frac = 0;
    int seen = 0;  // a non-zero digit
    uint32_t zeros = 0;  // zeros after the last non-zero digit, not yet stored
    d->neg = 0;
    d->n = 0;
    d->exp = 0;
    d->too_long = 0;
    if (i < n && lit[i] == '-') {
        d->neg = 1;
        i++;
    }
    int in_frac = 0;
    for (; i < n && (is_digit(lit[i]) || lit[i] == '.'); i++) {
        if (lit[i] == '.') {
            in_frac = 1;
            continue;
        }
        if (in_frac) frac++;
        if (lit[i] == '0') {
            if (seen) zeros++;
            continue;
        }
        seen = 1;
        while (zeros) {
            if (d->n < PY_MAX_DIGITS) d->digits[d->n++] = '0';
            else d->too_long = 1;
            zeros--;
        }
        if (d->n < PY_MAX_DIGITS) d->digits[d->n++] = lit[i];
        else d->too_long = 1;
    }
    int32_t e = 0;
    if (i < n && (lit[i] == 'e' || lit[i] == 'E')) {
        i++;
        int eneg = 0;
        if (i < n && (lit[i] == '+' || lit[i] == '-')) eneg = lit[i++] == '-';
        for (; i < n && is_digit(lit[i]); i++) {
            if (e < 10000000) e = e * 10 + (lit[i] - '0');
        }
        if (eneg) e = -e;
    }
    // value = digits (n of them) * 10**(e - frac + trailing zeros dropped)
    d->exp = e - frac + (int32_t)zeros;
}

size_t py_significant_digits(const char *lit, size_t n) {
    static struct decimal d;
    decimal_parse(lit, n, &d);
    return d.too_long ? PY_MAX_DIGITS + 1 : d.n;
}

void py_int(struct text *t, const char *lit, size_t n) {
    size_t i = 0;
    int neg = n && lit[0] == '-';
    if (neg) i++;
    while (i + 1 < n && lit[i] == '0') i++;
    if (neg && !(n - i == 1 && lit[i] == '0')) text_char(t, '-');
    text_mem(t, lit + i, n - i);
}

// ---- float ----------------------------------------------------------------------------------

#define MANT_MIN ((uint64_t)1 << 52)
#define MANT_LIM ((uint64_t)1 << 53)
#define E2_MIN (-1074)
#define E2_MAX 971

static struct big bn, bs, bt, br, bmp, bmm;

// The double nearest d (not zero, not too long): 0 with *m, *e2, or 1 for infinity.
// *m == 0 is an underflow to zero.
static int nearest_double(const struct decimal *d, uint64_t *m, int32_t *e2) {
    // floor(log2(value)) estimate from the first digits
    uint64_t lead = 0;
    uint32_t nl = 0;
    for (; nl < d->n && nl < 18; nl++) lead = lead * 10 + (uint64_t)(d->digits[nl] - '0');
    int32_t p10 = d->exp + (int32_t)(d->n - nl);  // value ~ lead * 10**p10
    int64_t est = (int64_t)bitlen64(lead) - 1 + ((int64_t)p10 * 3321928) / 1000000;
    int32_t e = (int32_t)(est - 52);
    if (e < E2_MIN) e = E2_MIN;
    for (int tries = 0; tries < 64; tries++) {
        // bn / bs = value / 2**e
        bn.n = 0;
        for (uint32_t i = 0; i < d->n; i++) {
            if (bn.n == 0) big_set(&bn, (uint64_t)(d->digits[i] - '0'));
            else big_mul_add(&bn, 10, (uint32_t)(d->digits[i] - '0'));
        }
        big_set(&bs, 1);
        if (d->exp >= 0) big_mul_pow10(&bn, (uint32_t)d->exp);
        else big_mul_pow10(&bs, (uint32_t)-d->exp);
        if (e >= 0) big_shl(&bs, (uint32_t)e);
        else big_shl(&bn, (uint32_t)-e);
        big_copy(&bt, &bs);
        big_shl(&bt, 53);  // bt = bs * 2**53
        if (big_cmp(&bn, &bt) >= 0) {  // quotient >= 2**53: e too small
            e++;
            continue;
        }
        big_shr1(&bt);  // bs * 2**52
        if (e > E2_MIN && big_cmp(&bn, &bt) < 0) {  // quotient < 2**52 and not subnormal
            e--;
            continue;
        }
        uint64_t q = 0;
        for (int bit = 52; bit >= 0; bit--) {  // bt = bs * 2**bit
            if (big_cmp(&bn, &bt) >= 0) {
                big_sub(&bn, &bt);
                q |= (uint64_t)1 << bit;
            }
            if (bit) big_shr1(&bt);
        }
        big_shl(&bn, 1);  // twice the remainder against bs
        int c = big_cmp(&bn, &bs);
        if (c > 0 || (c == 0 && (q & 1))) q++;
        if (q == MANT_LIM) {
            q = MANT_MIN;
            e++;
        }
        if (e > E2_MAX) return 1;
        *m = q;
        *e2 = e;
        return 0;
    }
    big_overflow = 1;
    return 1;
}

// The shortest digits of m * 2**e2 (m > 0): writes them to out, returns their count, sets
// *decpt so that the value is 0.digits * 10**decpt.
static uint32_t shortest(uint64_t m, int32_t e2, char *out, int32_t *decpt) {
    int even = (m & 1) == 0;
    int lower_closer = m == MANT_MIN && e2 > E2_MIN;  // the gap below is half the gap above
    if (e2 >= 0) {
        big_set(&br, m);
        big_shl(&br, (uint32_t)e2 + 1 + (uint32_t)lower_closer);
        big_set(&bs, lower_closer ? 4 : 2);
        big_set(&bmp, 1);
        big_shl(&bmp, (uint32_t)e2 + (uint32_t)lower_closer);
        big_set(&bmm, 1);
        big_shl(&bmm, (uint32_t)e2);
    } else {
        big_set(&br, m);
        big_shl(&br, 1 + (uint32_t)lower_closer);
        big_set(&bs, 1);
        big_shl(&bs, (uint32_t)(1 - e2) + (uint32_t)lower_closer);
        big_set(&bmp, lower_closer ? 2 : 1);
        big_set(&bmm, 1);
    }
    // k: a lower bound of ceil(log10(value)), raised below until the first digit is < 10
    int32_t l2 = e2 + (int32_t)bitlen64(m) - 1;  // floor(log2(value))
    int64_t prod = (int64_t)l2 * 30103;
    int32_t k = (int32_t)(prod >= 0 ? prod / 100000 : -((-prod + 99999) / 100000));
    if (k >= 0) {
        big_mul_pow10(&bs, (uint32_t)k);
    } else {
        big_mul_pow10(&br, (uint32_t)-k);
        big_mul_pow10(&bmp, (uint32_t)-k);
        big_mul_pow10(&bmm, (uint32_t)-k);
    }
    for (;;) {
        int c = big_cmp_sum(&br, &bmp, &bs);
        if (even ? c >= 0 : c > 0) {
            big_mul_add(&bs, 10, 0);
            k++;
        } else {
            break;
        }
    }
    uint32_t nd = 0;
    for (;;) {
        big_mul_add(&br, 10, 0);
        big_mul_add(&bmp, 10, 0);
        big_mul_add(&bmm, 10, 0);
        int digit = 0;
        while (big_cmp(&br, &bs) >= 0) {
            big_sub(&br, &bs);
            digit++;
        }
        int cl = big_cmp(&br, &bmm);
        int low = even ? cl <= 0 : cl < 0;
        int ch = big_cmp_sum(&br, &bmp, &bs);
        int high = even ? ch >= 0 : ch > 0;
        if (!low && !high) {
            out[nd++] = (char)('0' + digit);
            if (nd >= 40 || big_overflow) break;  // cannot happen: 17 digits always suffice
            continue;
        }
        if (low && high) {
            big_shl(&br, 1);
            int c2 = big_cmp(&br, &bs);
            if (c2 > 0 || (c2 == 0 && (digit & 1))) digit++;
        } else if (high) {
            digit++;
        }
        // a carry out of the last digit (a 9 rounded up)
        while (digit == 10) {
            if (nd == 0) {
                digit = 1;
                k++;
                break;
            }
            digit = out[--nd] - '0' + 1;
        }
        out[nd++] = (char)('0' + digit);
        break;
    }
    *decpt = k;
    return nd;
}

static void layout(struct text *t, int neg, const char *ds, uint32_t nd, int32_t decpt) {
    if (neg) text_char(t, '-');
    if (decpt > -4 && decpt <= 16) {
        if (decpt <= 0) {
            text_str(t, "0.");
            for (int32_t i = 0; i < -decpt; i++) text_char(t, '0');
            text_mem(t, ds, nd);
        } else if ((uint32_t)decpt >= nd) {
            text_mem(t, ds, nd);
            for (uint32_t i = nd; i < (uint32_t)decpt; i++) text_char(t, '0');
            text_str(t, ".0");
        } else {
            text_mem(t, ds, (size_t)decpt);
            text_char(t, '.');
            text_mem(t, ds + decpt, nd - (uint32_t)decpt);
        }
        return;
    }
    text_char(t, ds[0]);
    if (nd > 1) {
        text_char(t, '.');
        text_mem(t, ds + 1, nd - 1);
    }
    int32_t x = decpt - 1;
    text_char(t, 'e');
    text_char(t, x < 0 ? '-' : '+');
    if (x < 0) x = -x;
    if (x < 10) text_char(t, '0');
    text_u32(t, (uint32_t)x);
}

void py_float(struct text *t, const char *lit, size_t n) {
    if (n == 3 && lit[0] == 'N') {
        text_str(t, "nan");
        return;
    }
    if (n >= 8 && (lit[0] == 'I' || lit[1] == 'I')) {
        text_str(t, lit[0] == '-' ? "-inf" : "inf");
        return;
    }
    static struct decimal d;
    decimal_parse(lit, n, &d);
    if (d.too_long) {
        text_char(t, '?');
        return;
    }
    if (d.n == 0) {
        text_str(t, d.neg ? "-0.0" : "0.0");
        return;
    }
    int32_t x = (int32_t)d.n + d.exp;  // 10**(x-1) <= value < 10**x
    if (x > 310) {
        text_str(t, d.neg ? "-inf" : "inf");
        return;
    }
    if (x < -324) {
        text_str(t, d.neg ? "-0.0" : "0.0");
        return;
    }
    if (d.n <= 15 && x >= -306 && x <= 308) {
        layout(t, d.neg, d.digits, d.n, x);
        return;
    }
    uint64_t m;
    int32_t e2;
    big_overflow = 0;
    if (nearest_double(&d, &m, &e2)) {
        text_str(t, big_overflow ? "?" : d.neg ? "-inf" : "inf");
        return;
    }
    if (m == 0) {
        text_str(t, d.neg ? "-0.0" : "0.0");
        return;
    }
    char ds[40];
    int32_t decpt;
    uint32_t nd = shortest(m, e2, ds, &decpt);
    if (big_overflow) {
        text_char(t, '?');
        return;
    }
    layout(t, d.neg, ds, nd, decpt);
}

// ---- strings --------------------------------------------------------------------------------

// str.isprintable() of one code point: generated from the build's Python (py_printable.h).
#define NONPRINTABLE_IN(table, cp)                                                   \
    do {                                                                             \
        size_t lo = 0, hi = sizeof table / sizeof table[0];                          \
        while (lo < hi) {                                                            \
            size_t mid = lo + (hi - lo) / 2;                                         \
            if ((cp) < table[mid][0]) hi = mid;                                      \
            else if ((cp) > table[mid][1]) lo = mid + 1;                             \
            else return 0;                                                           \
        }                                                                            \
        return 1;                                                                    \
    } while (0)

int py_isprintable(uint32_t cp) {
    if (cp > 0x10ffff) return 0;
    if (cp <= 0xffff) NONPRINTABLE_IN(py_nonprintable_bmp, cp);
    NONPRINTABLE_IN(py_nonprintable_astral, cp);
}

void py_utf8(struct text *t, uint32_t cp) {
    if (cp < 0x80) {
        text_char(t, (char)cp);
    } else if (cp < 0x800) {
        text_char(t, (char)(0xc0 | (cp >> 6)));
        text_char(t, (char)(0x80 | (cp & 0x3f)));
    } else if (cp < 0x10000) {
        text_char(t, (char)(0xe0 | (cp >> 12)));
        text_char(t, (char)(0x80 | ((cp >> 6) & 0x3f)));
        text_char(t, (char)(0x80 | (cp & 0x3f)));
    } else {
        text_char(t, (char)(0xf0 | (cp >> 18)));
        text_char(t, (char)(0x80 | ((cp >> 12) & 0x3f)));
        text_char(t, (char)(0x80 | ((cp >> 6) & 0x3f)));
        text_char(t, (char)(0x80 | (cp & 0x3f)));
    }
}

void py_repr_char(struct text *t, uint32_t cp, char quote) {
    if (cp == (uint32_t)quote || cp == '\\') {
        text_char(t, '\\');
        text_char(t, (char)cp);
    } else if (cp == '\t') {
        text_str(t, "\\t");
    } else if (cp == '\n') {
        text_str(t, "\\n");
    } else if (cp == '\r') {
        text_str(t, "\\r");
    } else if (py_isprintable(cp)) {
        py_utf8(t, cp);
    } else if (cp < 0x100) {
        text_str(t, "\\x");
        text_hex(t, cp, 2);
    } else if (cp < 0x10000) {
        text_str(t, "\\u");
        text_hex(t, cp, 4);
    } else {
        text_str(t, "\\U");
        text_hex(t, cp, 8);
    }
}
