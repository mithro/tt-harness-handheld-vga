// SPDX-License-Identifier: Apache-2.0
//
// demo.json: UTF-8 check, JSON (jsmn tokens, then this file's checks of everything jsmn
// does not check), and the validator of tt_harness/demo/validate.py, check for check and
// message for message, values printed with Python's str() (pyrepr.c). See demo_parse.h.
//
// Python's JSON reader is the reference for what is JSON: numbers
// -?(0|[1-9][0-9]*)(.[0-9]+)?([eE][+-]?[0-9]+)?, the literals true, false, null, NaN,
// Infinity, -Infinity, strings without raw control characters (any \u escape, lone
// surrogates included), whitespace " \t\n\r", one value. validate.parse also refuses an
// object that repeats a key, with "duplicate key 'k'" for the first object to close that
// has one; so does this.

#include "demo_parse.h"

#include "pyrepr.h"
#include "text.h"

#define JSMN_STATIC
#include "third_party/jsmn.h"

#define LIMIT DEMO_LIMIT_PREFIX

static jsmntok_t tok[DEMO_MAX_TOKENS];
static const char *js;      // the text
static size_t js_len;
static int ntok;
static struct errors *E;

// ---- messages -------------------------------------------------------------------------------

static void errors_reset(struct errors *e) {
    e->count = 0;
    e->used = 0;
    e->not_shown = 0;
    e->summary_at = 0;
    e->text[0] = '\0';
}

const char *errors_get(const struct errors *e, uint32_t i, uint32_t *len) {
    if (i >= e->count) return 0;
    const unsigned char *p = (const unsigned char *)e->text;
    while (i--) p += 2 + (p[0] | p[1] << 8) + 1;
    *len = p[0] | (uint32_t)p[1] << 8;
    return (const char *)p + 2;
}

// A message is built in place at the end of the text. It is kept if it fits with
// DEMO_NOT_SHOWN_ROOM bytes still free; one that does not is left out and counted, and the
// last message is then DEMO_MSG_NOT_SHOWN with the count (tt_harness.soc.limits.stored).
static struct text msg;

static struct text *msg_begin(void) {
    if (E->not_shown) {  // take the count message off the end; msg_end puts it back
        E->used = E->summary_at;
        E->count--;
    }
    size_t room = DEMO_ERRORS_TEXT - E->used - DEMO_NOT_SHOWN_ROOM;  // >= 0: always kept free
    // the length (2), the message (at most cap - 1 bytes) and its NUL: room bytes at most
    text_init(&msg, E->text + E->used + 2, room > 2 ? room - 2 : 0);
    return &msg;
}

static void keep_message(size_t len) {
    E->text[E->used] = (char)(len & 0xff);
    E->text[E->used + 1] = (char)(len >> 8);
    E->used += (uint32_t)(2 + len + 1);
    E->count++;
}

static void msg_end(void) {
    if (msg.full) {
        E->not_shown++;
    } else {
        keep_message(msg.len);
    }
    if (E->not_shown) {
        struct text t;
        E->summary_at = E->used;
        text_init(&t, E->text + E->used + 2, DEMO_NOT_SHOWN_ROOM - 2);
        text_str(&t, DEMO_MSG_NOT_SHOWN);
        text_u32(&t, E->not_shown);
        keep_message(t.len);
    }
}

static void add_error(const char *s) {
    text_str(msg_begin(), s);
    msg_end();
}

void errors_add(struct errors *errs, const char *message) {
    E = errs;
    add_error(message);
}

// ---- UTF-8, with Python's error messages ----------------------------------------------------

static int utf8_check(const unsigned char *s, size_t n) {
    for (size_t i = 0; i < n;) {
        unsigned char c = s[i];
        if (c < 0x80) {
            i++;
            continue;
        }
        size_t need;
        unsigned char lo = 0x80, hi = 0xbf;
        if (c >= 0xc2 && c <= 0xdf) need = 1;
        else if (c >= 0xe0 && c <= 0xef) {
            need = 2;
            if (c == 0xe0) lo = 0xa0;
            if (c == 0xed) hi = 0x9f;
        } else if (c >= 0xf0 && c <= 0xf4) {
            need = 3;
            if (c == 0xf0) lo = 0x90;
            if (c == 0xf4) hi = 0x8f;
        } else {
            need = 0;
        }
        const char *why = 0;
        size_t end = i + 1;
        if (need == 0) {
            why = "invalid start byte";
        } else {
            for (size_t k = 1; k <= need; k++) {
                if (i + k >= n) {
                    why = "unexpected end of data";
                    end = n;
                    break;
                }
                unsigned char b = s[i + k];
                unsigned char l = k == 1 ? lo : 0x80, h = k == 1 ? hi : 0xbf;
                if (b < l || b > h) {
                    why = "invalid continuation byte";
                    end = i + k;
                    break;
                }
            }
        }
        if (!why) {
            i += need + 1;
            continue;
        }
        struct text *t = msg_begin();
        text_str(t, "not UTF-8: 'utf-8' codec can't decode ");
        if (end - i == 1) {
            text_str(t, "byte 0x");
            text_hex(t, c, 2);
            text_str(t, " in position ");
            text_u32(t, (uint32_t)i);
        } else {
            text_str(t, "bytes in position ");
            text_u32(t, (uint32_t)i);
            text_char(t, '-');
            text_u32(t, (uint32_t)(end - 1));
        }
        text_str(t, ": ");
        text_str(t, why);
        msg_end();
        return 0;
    }
    return 1;
}

// ---- tokens ---------------------------------------------------------------------------------

// The text a token covers, quotes included for a string.
static int tok_from(int i) { return tok[i].type == JSMN_STRING ? tok[i].start - 1 : tok[i].start; }
static int tok_to(int i) { return tok[i].type == JSMN_STRING ? tok[i].end + 1 : tok[i].end; }

// The index after token i and everything inside it.
static int skip(int i) {
    int n = tok[i].size;
    int j = i + 1;
    if (tok[i].type == JSMN_OBJECT) {
        for (int k = 0; k < n; k++) j = skip(j + 1);
    } else if (tok[i].type == JSMN_ARRAY) {
        for (int k = 0; k < n; k++) j = skip(j);
    }
    return j;
}

static int is_ws(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'; }

// The code points of a JSON string token, escapes decoded (the text is valid UTF-8).
struct jstr {
    int pos, end;
};

static void jstr_init(struct jstr *s, int i) {
    s->pos = tok[i].start;
    s->end = tok[i].end;
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int hex4(int at, uint32_t *v) {
    if (at + 4 > (int)js_len) return 0;
    uint32_t r = 0;
    for (int k = 0; k < 4; k++) {
        int h = hexval(js[at + k]);
        if (h < 0) return 0;
        r = r << 4 | (uint32_t)h;
    }
    *v = r;
    return 1;
}

// 1 and the next code point; 0 at the end; -1 for a bad escape or a raw control character.
static int jstr_next(struct jstr *s, uint32_t *cp) {
    if (s->pos >= s->end) return 0;
    unsigned char c = (unsigned char)js[s->pos];
    if (c < 0x20) return -1;
    if (c == '\\') {
        if (s->pos + 1 >= s->end) return -1;
        char e = js[s->pos + 1];
        s->pos += 2;
        switch (e) {
            case '"': *cp = '"'; return 1;
            case '\\': *cp = '\\'; return 1;
            case '/': *cp = '/'; return 1;
            case 'b': *cp = '\b'; return 1;
            case 'f': *cp = '\f'; return 1;
            case 'n': *cp = '\n'; return 1;
            case 'r': *cp = '\r'; return 1;
            case 't': *cp = '\t'; return 1;
            case 'u': {
                uint32_t u, lo;
                if (s->pos + 4 > s->end || !hex4(s->pos, &u)) return -1;
                s->pos += 4;
                // a high surrogate followed by \u and a low one is one code point
                if (u >= 0xd800 && u <= 0xdbff && s->pos + 6 <= s->end && js[s->pos] == '\\' &&
                    js[s->pos + 1] == 'u' && hex4(s->pos + 2, &lo) && lo >= 0xdc00 &&
                    lo <= 0xdfff) {
                    s->pos += 6;
                    u = 0x10000 + ((u - 0xd800) << 10) + (lo - 0xdc00);
                }
                *cp = u;
                return 1;
            }
            default: return -1;
        }
    }
    // UTF-8 (already checked)
    int n = c < 0x80 ? 0 : c < 0xe0 ? 1 : c < 0xf0 ? 2 : 3;
    uint32_t v = n == 0 ? c : n == 1 ? (c & 0x1fu) : n == 2 ? (c & 0x0fu) : (c & 0x07u);
    for (int k = 1; k <= n; k++) v = v << 6 | ((unsigned char)js[s->pos + k] & 0x3fu);
    s->pos += n + 1;
    *cp = v;
    return 1;
}

// String token i equals the ASCII text s.
static int str_eq(int i, const char *s) {
    if (tok[i].type != JSMN_STRING) return 0;
    struct jstr it;
    jstr_init(&it, i);
    uint32_t cp;
    while (jstr_next(&it, &cp) == 1) {
        if (*s == '\0' || (uint32_t)(unsigned char)*s != cp) return 0;
        s++;
    }
    return *s == '\0';
}

static int str_eq_tok(int a, int b) {
    struct jstr x, y;
    jstr_init(&x, a);
    jstr_init(&y, b);
    for (;;) {
        uint32_t ca, cb;
        int ra = jstr_next(&x, &ca), rb = jstr_next(&y, &cb);
        if (ra != rb) return 0;
        if (ra != 1) return 1;
        if (ca != cb) return 0;
    }
}

// ---- JSON checks ----------------------------------------------------------------------------

struct where {
    int at;  // offset of the problem
    const char *what;
};
static struct where bad;
static int dup_obj;    // the object with a repeated key that closes first, or -1
static int dup_key;    // its first repeated key
static int too_deep;
static int too_long;   // a number with more significant digits than pyrepr handles

static int ws_only(int from, int to) {
    for (int p = from; p < to; p++) {
        if (!is_ws(js[p])) return 0;
    }
    return 1;
}

// The text from..to is whitespace, sep, whitespace.
static int separated(int from, int to, char sep) {
    int p = from;
    while (p < to && is_ws(js[p])) p++;
    if (p >= to || js[p] != sep) return 0;
    return ws_only(p + 1, to);
}

static int fail(int at, const char *what) {
    if (!bad.what) {
        bad.at = at;
        bad.what = what;
    }
    return 0;
}

static int lit_is(int i, const char *s) {
    int n = tok[i].end - tok[i].start;
    for (int k = 0; k < n; k++) {
        if (s[k] == '\0' || js[tok[i].start + k] != s[k]) return 0;
    }
    return s[n] == '\0';
}

static int number_ok(int from, int to) {
    int p = from;
    if (p < to && js[p] == '-') p++;
    if (p >= to) return 0;
    if (js[p] == '0') {
        p++;
    } else if (js[p] >= '1' && js[p] <= '9') {
        while (p < to && js[p] >= '0' && js[p] <= '9') p++;
    } else {
        return 0;
    }
    if (p < to && js[p] == '.') {
        p++;
        if (p >= to || js[p] < '0' || js[p] > '9') return 0;
        while (p < to && js[p] >= '0' && js[p] <= '9') p++;
    }
    if (p < to && (js[p] == 'e' || js[p] == 'E')) {
        p++;
        if (p < to && (js[p] == '+' || js[p] == '-')) p++;
        if (p >= to || js[p] < '0' || js[p] > '9') return 0;
        while (p < to && js[p] >= '0' && js[p] <= '9') p++;
    }
    return p == to;
}

static int is_number(int i) {
    return tok[i].type == JSMN_PRIMITIVE && number_ok(tok[i].start, tok[i].end);
}

// A number with no fraction or exponent: Python's int.
static int is_intlit(int i) {
    if (!is_number(i)) return 0;
    for (int p = tok[i].start; p < tok[i].end; p++) {
        if (js[p] == '.' || js[p] == 'e' || js[p] == 'E') return 0;
    }
    return 1;
}

// Check the value at token i; the index after it, or -1.
static int check_value(int i, int depth) {
    if (i >= ntok) return fail((int)js_len, "expecting a value"), -1;
    if (depth > DEMO_MAX_DEPTH) {
        too_deep = 1;
        return -1;
    }
    jsmntok_t *t = &tok[i];
    if (t->type == JSMN_PRIMITIVE) {
        if (t->size != 0) return fail(t->end, "unexpected ':'"), -1;
        if (lit_is(i, "true") || lit_is(i, "false") || lit_is(i, "null") || lit_is(i, "NaN") ||
            lit_is(i, "Infinity") || lit_is(i, "-Infinity")) {
            return i + 1;
        }
        if (!number_ok(t->start, t->end)) return fail(t->start, "not a value"), -1;
        if (py_significant_digits(js + t->start, (size_t)(t->end - t->start)) > PY_MAX_DIGITS) {
            too_long = 1;
        }
        return i + 1;
    }
    if (t->type == JSMN_STRING) {
        if (t->size != 0) return fail(t->end + 1, "unexpected ':'"), -1;
        struct jstr s;
        jstr_init(&s, i);
        uint32_t cp;
        int r;
        while ((r = jstr_next(&s, &cp)) == 1) {
        }
        if (r < 0) return fail(s.pos, "invalid control character or escape in a string"), -1;
        return i + 1;
    }
    if (t->type == JSMN_ARRAY) {
        int p = t->start + 1;
        int j = i + 1;
        for (int k = 0; k < t->size; k++) {
            if (j >= ntok) return fail(t->end, "expecting a value"), -1;
            int from = tok_from(j);
            if (k == 0 ? !ws_only(p, from) : !separated(p, from, ',')) {
                return fail(p, "expecting ',' between values"), -1;
            }
            int next = check_value(j, depth + 1);
            if (next < 0) return -1;
            p = tok_to(j);
            j = next;
        }
        if (!ws_only(p, t->end - 1)) return fail(p, "expecting ']' or a value"), -1;
        return j;
    }
    if (t->type == JSMN_OBJECT) {
        int p = t->start + 1;
        int j = i + 1;
        for (int k = 0; k < t->size; k++) {
            if (j >= ntok) return fail(t->end, "expecting a key"), -1;
            if (tok[j].type != JSMN_STRING || tok[j].size != 1) {
                return fail(tok_from(j), "expecting a string key and ':'"), -1;
            }
            int from = tok_from(j);
            if (k == 0 ? !ws_only(p, from) : !separated(p, from, ',')) {
                return fail(p, "expecting ',' between members"), -1;
            }
            struct jstr s;
            jstr_init(&s, j);
            uint32_t cp;
            int r;
            while ((r = jstr_next(&s, &cp)) == 1) {
            }
            if (r < 0) return fail(s.pos, "invalid control character or escape in a string"), -1;
            int v = j + 1;
            if (v >= ntok) return fail(t->end, "expecting a value"), -1;
            if (!separated(tok_to(j), tok_from(v), ':')) {
                return fail(tok_to(j), "expecting ':'"), -1;
            }
            int next = check_value(v, depth + 1);
            if (next < 0) return -1;
            p = tok_to(v);
            j = next;
        }
        if (!ws_only(p, t->end - 1)) return fail(p, "expecting '}' or a member"), -1;
        // a repeated key: Python's hook sees each object when it closes
        int key = j > i + 1 ? i + 1 : -1;
        for (int k = 0; k < t->size; k++) {
            for (int a = i + 1; a < key; a = skip(a + 1)) {
                if (str_eq_tok(a, key) && (dup_obj < 0 || t->end < tok[dup_obj].end)) {
                    dup_obj = i;
                    dup_key = key;
                    goto found;
                }
            }
            key = skip(key + 1);
        }
    found:
        return j;
    }
    return fail(t->start, "not a value"), -1;
}

// Line and column (1-based) of offset at, as Python reports them.
static void where_text(struct text *t, int at) {
    uint32_t line = 1, col = 1;
    for (int p = 0; p < at && p < (int)js_len; p++) {
        if (js[p] == '\n') {
            line++;
            col = 1;
        } else {
            col++;
        }
    }
    text_str(t, ": line ");
    text_u32(t, line);
    text_str(t, " column ");
    text_u32(t, col);
    text_str(t, " (char ");
    text_u32(t, (uint32_t)at);
    text_char(t, ')');
}

static void value_str(struct text *t, int i, int top);

static void not_json(const char *what, int at) {
    struct text *t = msg_begin();
    text_str(t, "not valid JSON: ");
    text_str(t, what);
    where_text(t, at);
    msg_end();
}

// Tokenise and check; 1 if the text is one valid JSON value (token 0).
static int json_ok(void) {
    for (size_t p = 0; p < js_len; p++) {
        if (js[p] == '\0') return not_json("a NUL byte", (int)p), 0;
    }
    jsmn_parser parser;
    jsmn_init(&parser);
    ntok = jsmn_parse(&parser, js, js_len, tok, DEMO_MAX_TOKENS);
    if (ntok == JSMN_ERROR_NOMEM) {
        add_error(DEMO_MSG_TOKENS);
        return 0;
    }
    if (ntok < 0) {
        not_json(ntok == JSMN_ERROR_PART ? "unexpected end of the text" : "unexpected character",
                 (int)parser.pos);
        return 0;
    }
    if (ntok == 0) return not_json("expecting a value", (int)js_len), 0;
    bad.what = 0;
    dup_obj = -1;
    too_deep = 0;
    too_long = 0;
    if (!ws_only(0, tok_from(0))) return not_json("expecting a value", 0), 0;
    int next = check_value(0, 0);
    if (too_deep) {
        add_error(DEMO_MSG_DEPTH);
        return 0;
    }
    if (next < 0) return not_json(bad.what, bad.at), 0;
    if (next != ntok || !ws_only(tok_to(0), (int)js_len)) {
        int at = tok_to(0);
        while (at < (int)js_len && is_ws(js[at])) at++;
        return not_json("extra data", at), 0;
    }
    if (dup_obj >= 0) {
        struct text *t = msg_begin();
        text_str(t, "not valid JSON: duplicate key ");
        value_str(t, dup_key, 0);
        msg_end();
        return 0;
    }
    if (too_long) {
        add_error(DEMO_MSG_DIGITS);
        return 0;
    }
    return 1;
}

// ---- Python's str() of a value --------------------------------------------------------------

static void string_repr(struct text *t, int i) {
    struct jstr s;
    uint32_t cp;
    int single = 0, dbl = 0;
    jstr_init(&s, i);
    while (jstr_next(&s, &cp) == 1) {
        if (cp == '\'') single = 1;
        if (cp == '"') dbl = 1;
    }
    char quote = single && !dbl ? '"' : '\'';
    text_char(t, quote);
    jstr_init(&s, i);
    while (jstr_next(&s, &cp) == 1) py_repr_char(t, cp, quote);
    text_char(t, quote);
}

// str(value) when top, else repr(value) (inside a list or a dict).
static void value_str(struct text *t, int i, int top) {
    jsmntok_t *k = &tok[i];
    if (k->type == JSMN_STRING) {
        if (!top) {
            string_repr(t, i);
            return;
        }
        struct jstr s;
        uint32_t cp;
        jstr_init(&s, i);
        while (jstr_next(&s, &cp) == 1) py_utf8(t, cp);
        return;
    }
    if (k->type == JSMN_PRIMITIVE) {
        const char *p = js + k->start;
        size_t n = (size_t)(k->end - k->start);
        if (lit_is(i, "true")) text_str(t, "True");
        else if (lit_is(i, "false")) text_str(t, "False");
        else if (lit_is(i, "null")) text_str(t, "None");
        else if (is_intlit(i)) py_int(t, p, n);
        else py_float(t, p, n);
        return;
    }
    int j = i + 1;
    if (k->type == JSMN_ARRAY) {
        text_char(t, '[');
        for (int c = 0; c < k->size; c++) {
            if (c) text_str(t, ", ");
            value_str(t, j, 0);
            j = skip(j);
        }
        text_char(t, ']');
        return;
    }
    text_char(t, '{');
    for (int c = 0; c < k->size; c++) {
        if (c) text_str(t, ", ");
        string_repr(t, j);
        text_str(t, ": ");
        value_str(t, j + 1, 0);
        j = skip(j + 1);
    }
    text_char(t, '}');
}

// ---- integers of any size -------------------------------------------------------------------

// The integer literal at token i (is_intlit): sign and digits without leading zeros.
struct jint {
    int neg;
    const char *d;
    int n;
};

static struct jint jint_of(int i) {
    struct jint v;
    const char *p = js + tok[i].start;
    int n = tok[i].end - tok[i].start;
    v.neg = 0;
    if (*p == '-') {
        p++;
        n--;
        v.neg = 1;
    }
    v.d = p;
    v.n = n;  // JSON has no leading zeros: "0" or [1-9][0-9]*
    if (n == 1 && *p == '0') v.neg = 0;
    return v;
}

static int jint_zero(struct jint v) { return v.n == 1 && v.d[0] == '0'; }

// 0 <= v <= limit (a negative v is not: 0).
static int jint_le(struct jint v, uint32_t limit) {
    if (v.neg) return 0;
    if (v.n > 10) return 0;
    uint64_t x = 0;
    for (int k = 0; k < v.n; k++) x = x * 10 + (uint64_t)(v.d[k] - '0');
    return x <= limit;
}

static uint32_t jint_u32(struct jint v) {
    uint32_t x = 0;
    for (int k = 0; k < v.n; k++) x = x * 10 + (uint32_t)(v.d[k] - '0');
    return x;
}

// v & 0x70 != 0, two's complement for a negative v as Python's & is.
static int jint_pad_bits(struct jint v) {
    uint32_t m = 0;  // |v| mod 128
    for (int k = 0; k < v.n; k++) m = (m * 10 + (uint32_t)(v.d[k] - '0')) % 128;
    if (v.neg) m = (128 - m) % 128;
    return (m & 0x70) != 0;
}

// ---- objects --------------------------------------------------------------------------------

static int obj_get(int obj, const char *key) {
    int j = obj + 1;
    for (int k = 0; k < tok[obj].size; k++) {
        if (str_eq(j, key)) return j + 1;
        j = skip(j + 1);
    }
    return -1;
}

static int obj_len(int obj) { return tok[obj].size; }

// ---- the validator (tt_harness/demo/validate.py) --------------------------------------------

static const char *const TOP_FIELDS[] = {"version", "shuttle", "projects"};
static const char *const ENTRY_FIELDS[] = {"address", "subtile", "duration_ms", "clock_hz",
                                           "ui_in",   "steps"};
static const char *const BUTTONS[] = {"b",    "y",    "select", "start", "up", "down",
                                      "left", "right", "a",     "x",     "l",  "r"};
#define N_BUTTONS 12
#define GAMEPAD_PINS 0x70

static int known(int key, const char *const *names, int n) {
    for (int k = 0; k < n; k++) {
        if (str_eq(key, names[k])) return 1;
    }
    return 0;
}

static int button_index(int i) {
    for (int k = 0; k < N_BUTTONS; k++) {
        if (str_eq(i, BUTTONS[k])) return k;
    }
    return -1;
}

static void where_step(struct text *t, int entry, int step) {
    text_str(t, "project ");
    text_u32(t, (uint32_t)entry);
    if (step >= 0) {
        text_str(t, " step ");
        text_u32(t, (uint32_t)step);
    }
}

static int uses_gamepad(int steps) {
    if (tok[steps].type != JSMN_ARRAY) return 0;
    int j = steps + 1;
    for (int k = 0; k < tok[steps].size; k++) {
        if (tok[j].type == JSMN_OBJECT && obj_len(j) == 1 && obj_get(j, "gamepad") >= 0) return 1;
        j = skip(j);
    }
    return 0;
}

static void check_gamepad(int entry, int step, int v) {
    struct text *t;
    if (tok[v].type != JSMN_ARRAY || tok[v].size > 2) {
        t = msg_begin();
        where_step(t, entry, step);
        text_str(t, ": \"gamepad\" must be a list of 0 to 2 controllers, got ");
        value_str(t, v, 1);
        msg_end();
        return;
    }
    int c = v + 1;
    for (int j = 0; j < tok[v].size; j++, c = skip(c)) {
        if (tok[c].type != JSMN_OBJECT || obj_len(c) != 1 || obj_get(c, "buttons") < 0) {
            t = msg_begin();
            where_step(t, entry, step);
            text_str(t, ": gamepad controller ");
            text_u32(t, (uint32_t)j + 1);
            text_str(t, " must be an object with exactly one key, \"buttons\": ");
            value_str(t, c, 1);
            msg_end();
            continue;
        }
        int b = obj_get(c, "buttons");
        if (tok[b].type != JSMN_ARRAY) {
            t = msg_begin();
            where_step(t, entry, step);
            text_str(t, ": gamepad controller ");
            text_u32(t, (uint32_t)j + 1);
            text_str(t, ": \"buttons\" must be a list, got ");
            value_str(t, b, 1);
            msg_end();
            continue;
        }
        int unknown = 0;
        int x = b + 1;
        for (int k = 0; k < tok[b].size; k++, x = skip(x)) {
            if (button_index(x) >= 0) continue;
            unknown = 1;
            t = msg_begin();
            where_step(t, entry, step);
            text_str(t, ": gamepad controller ");
            text_u32(t, (uint32_t)j + 1);
            text_str(t, ": unknown button ");
            value_str(t, x, 1);
            msg_end();
        }
        if (unknown) continue;
        int ordered = 1, last = -1;
        x = b + 1;
        for (int k = 0; k < tok[b].size; k++, x = skip(x)) {
            int idx = button_index(x);
            if (k > 0 && idx <= last) ordered = 0;
            last = idx;
        }
        if (!ordered) {
            t = msg_begin();
            where_step(t, entry, step);
            text_str(t, ": gamepad controller ");
            text_u32(t, (uint32_t)j + 1);
            text_str(t, ": \"buttons\" must name each button once, in the order ");
            for (int k = 0; k < N_BUTTONS; k++) {
                if (k) text_str(t, ", ");
                text_str(t, BUTTONS[k]);
            }
            text_str(t, ", got ");
            value_str(t, b, 1);
            msg_end();
        } else if (tok[b].size == N_BUTTONS) {
            t = msg_begin();
            where_step(t, entry, step);
            text_str(t, ": gamepad controller ");
            text_u32(t, (uint32_t)j + 1);
            text_str(t, ": all 12 buttons at once reads as no controller");
            msg_end();
        }
    }
}

static void field_error(int entry, int step, const char *field, const char *what, int v) {
    struct text *t = msg_begin();
    where_step(t, entry, step);
    text_str(t, ": \"");
    text_str(t, field);
    text_str(t, "\" ");
    text_str(t, what);
    text_str(t, ", got ");
    value_str(t, v, 1);
    msg_end();
}

static void check_steps(int entry, int steps) {
    struct text *t;
    if (tok[steps].type != JSMN_ARRAY) {
        t = msg_begin();
        where_step(t, entry, -1);
        text_str(t, ": \"steps\" must be a list");
        msg_end();
        return;
    }
    int pad = uses_gamepad(steps);
    int waits = 0;  // some wait_ms is > 0 (the total is > 0)
    int s = steps + 1;
    for (int i = 0; i < tok[steps].size; i++, s = skip(s)) {
        if (tok[s].type != JSMN_OBJECT || obj_len(s) != 1) {
            t = msg_begin();
            where_step(t, entry, i);
            text_str(t, ": must be an object with exactly one key: ");
            value_str(t, s, 1);
            msg_end();
            continue;
        }
        int v;
        if ((v = obj_get(s, "set")) >= 0) {
            if (!is_intlit(v) || jint_of(v).neg || !jint_le(jint_of(v), 255)) {
                field_error(entry, i, "set", "must be an integer 0..255", v);
            } else if (pad && (jint_u32(jint_of(v)) & GAMEPAD_PINS)) {
                field_error(entry, i, "set", "must keep bits 4..6 (the gamepad) clear", v);
            }
        } else if ((v = obj_get(s, "gamepad")) >= 0) {
            check_gamepad(entry, i, v);
        } else if ((v = obj_get(s, "wait_ms")) >= 0) {
            if (!is_intlit(v) || jint_of(v).neg) {
                field_error(entry, i, "wait_ms", "must be an integer >= 0", v);
            } else if (!jint_zero(jint_of(v))) {
                waits = 1;
            }
        } else {
            t = msg_begin();
            where_step(t, entry, i);
            text_str(t, ": unknown step ");
            value_str(t, s, 1);
            msg_end();
        }
    }
    if (tok[steps].size > 0 && !waits) {
        t = msg_begin();
        where_step(t, entry, -1);
        text_str(t, ": \"steps\" must wait for some time in total");
        msg_end();
    }
}

static void check_entry(int entry, int e) {
    struct text *t;
    if (tok[e].type != JSMN_OBJECT) {
        t = msg_begin();
        where_step(t, entry, -1);
        text_str(t, ": must be an object");
        msg_end();
        return;
    }
    int ok = 1;
    int j = e + 1;
    for (int k = 0; k < tok[e].size; k++, j = skip(j + 1)) {
        if (known(j, ENTRY_FIELDS, 6)) continue;
        t = msg_begin();
        where_step(t, entry, -1);
        text_str(t, ": unknown field \"");
        value_str(t, j, 1);
        text_char(t, '"');
        msg_end();
        ok = 0;
    }
    for (int f = 0; f < 6; f++) {
        if (obj_get(e, ENTRY_FIELDS[f]) >= 0) continue;
        t = msg_begin();
        where_step(t, entry, -1);
        text_str(t, ": missing \"");
        text_str(t, ENTRY_FIELDS[f]);
        text_char(t, '"');
        msg_end();
        ok = 0;
    }
    if (!ok) return;
    static const struct {
        const char *field;
        const char *what;
        int min;  // 0: >= 0; 1: > 0
        uint32_t max;
    } checks[] = {
        {"address", "must be an integer >= 0", 0, 0},
        {"subtile", "must be an integer >= 0", 0, 0},
        {"duration_ms", "must be an integer > 0", 1, 0},
        {"clock_hz", "must be an integer >= 0", 0, 0},
        {"ui_in", "must be an integer 0..255", 0, 255},
    };
    for (int c = 0; c < 5; c++) {
        int v = obj_get(e, checks[c].field);
        int good = is_intlit(v) && !jint_of(v).neg;
        if (good && checks[c].min && jint_zero(jint_of(v))) good = 0;
        if (good && checks[c].max && !jint_le(jint_of(v), checks[c].max)) good = 0;
        if (!good) field_error(entry, -1, checks[c].field, checks[c].what, v);
    }
    int steps = obj_get(e, "steps");
    check_steps(entry, steps);
    if (uses_gamepad(steps)) {
        int ui = obj_get(e, "ui_in");
        if (is_intlit(ui) && jint_pad_bits(jint_of(ui))) {
            field_error(entry, -1, "ui_in", "must keep bits 4..6 (the gamepad) clear", ui);
        }
        int clk = obj_get(e, "clock_hz");
        if (is_intlit(clk) && jint_zero(jint_of(clk))) {
            t = msg_begin();
            where_step(t, entry, -1);
            text_str(t, ": \"clock_hz\" must be > 0 with \"gamepad\" steps: "
                        "the design reads the gamepad on its clock");
            msg_end();
        }
    }
}

static void validate(const char *shuttle) {
    struct text *t;
    if (tok[0].type != JSMN_OBJECT) {
        add_error("must be a JSON object");
        return;
    }
    int j = 1;
    for (int k = 0; k < tok[0].size; k++, j = skip(j + 1)) {
        if (known(j, TOP_FIELDS, 3)) continue;
        t = msg_begin();
        text_str(t, "unknown field \"");
        value_str(t, j, 1);
        text_char(t, '"');
        msg_end();
    }
    int missing = 0;
    for (int f = 0; f < 3; f++) {
        if (obj_get(0, TOP_FIELDS[f]) >= 0) continue;
        t = msg_begin();
        text_str(t, "missing \"");
        text_str(t, TOP_FIELDS[f]);
        text_char(t, '"');
        msg_end();
        missing = 1;
    }
    if (missing) return;
    int v = obj_get(0, "version");
    if (!is_intlit(v) || jint_of(v).neg || jint_of(v).n != 1 || jint_of(v).d[0] != '1') {
        t = msg_begin();
        text_str(t, "\"version\" must be 1, got ");
        value_str(t, v, 1);
        msg_end();
    }
    int s = obj_get(0, "shuttle");
    if (!str_eq(s, shuttle)) {
        t = msg_begin();
        text_str(t, "\"shuttle\" is \"");
        value_str(t, s, 1);
        text_str(t, "\" but this chip is \"");
        text_str(t, shuttle);
        text_char(t, '"');
        msg_end();
    }
    int p = obj_get(0, "projects");
    if (tok[p].type != JSMN_ARRAY || tok[p].size == 0) {
        add_error("\"projects\" must be a non-empty list");
        return;
    }
    int e = p + 1;
    for (int i = 0; i < tok[p].size; i++, e = skip(e)) check_entry(i, e);
}

// ---- the parsed demo ------------------------------------------------------------------------

static int get_u32(int entry, int step, int v, const char *field, uint32_t max, uint32_t *out) {
    struct jint x = jint_of(v);
    if (jint_le(x, max)) {
        *out = jint_u32(x);
        return 1;
    }
    struct text *t = msg_begin();
    text_str(t, LIMIT);
    where_step(t, entry, step);
    text_str(t, DEMO_VALUE_FIELD);
    text_str(t, field);
    text_str(t, DEMO_VALUE_IS);
    value_str(t, v, 1);
    text_str(t, DEMO_VALUE_MORE);
    text_u32(t, max);
    msg_end();
    return 0;
}

static uint32_t gamepad_word(int v) {
    uint32_t mask[2] = {0, 0};
    int c = v + 1;
    for (int j = 0; j < tok[v].size; j++, c = skip(c)) {
        int b = obj_get(c, "buttons");
        int x = b + 1;
        for (int k = 0; k < tok[b].size; k++, x = skip(x)) mask[j] |= 1u << button_index(x);
    }
    int n = tok[v].size;
    uint32_t second = n >= 2 ? mask[1] : 0xfff;  // wire positions 0..11
    uint32_t first = n >= 1 ? mask[0] : 0xfff;   // wire positions 12..23
    return second | first << 12;
}

static void extract(const char *shuttle, struct demo *out) {
    int p = obj_get(0, "projects");
    out->version = 1;
    size_t k = 0;
    for (; shuttle[k] && k + 1 < DEMO_SHUTTLE_MAX; k++) out->shuttle[k] = shuttle[k];
    out->shuttle[k] = '\0';
    if (shuttle[k]) {
        add_error(DEMO_MSG_SHUTTLE);
        return;
    }
    if (tok[p].size > DEMO_MAX_PROJECTS) {
        add_error(DEMO_MSG_PROJECTS);
        return;
    }
    out->project_count = (uint32_t)tok[p].size;
    out->step_count = 0;
    int e = p + 1;
    for (int i = 0; i < tok[p].size; i++, e = skip(e)) {
        struct project *pr = &out->projects[i];
        uint32_t ui;
        get_u32(i, -1, obj_get(e, "address"), "address", DEMO_MAX_U32, &pr->address);
        get_u32(i, -1, obj_get(e, "subtile"), "subtile", DEMO_MAX_U32, &pr->subtile);
        get_u32(i, -1, obj_get(e, "duration_ms"), "duration_ms", DEMO_MAX_MS, &pr->duration_ms);
        get_u32(i, -1, obj_get(e, "clock_hz"), "clock_hz", DEMO_MAX_U32, &pr->clock_hz);
        get_u32(i, -1, obj_get(e, "ui_in"), "ui_in", 255, &ui);
        pr->ui_in = (uint8_t)ui;
        int steps = obj_get(e, "steps");
        pr->first_step = (uint16_t)out->step_count;
        pr->step_count = (uint16_t)tok[steps].size;
        if (out->step_count + (uint32_t)tok[steps].size > DEMO_MAX_STEPS) {
            add_error(DEMO_MSG_STEPS);
            return;
        }
        int s = steps + 1;
        for (int n = 0; n < tok[steps].size; n++, s = skip(s)) {
            struct step *st = &out->steps[out->step_count++];
            int v;
            if ((v = obj_get(s, "set")) >= 0) {
                st->kind = STEP_SET;
                st->value = jint_u32(jint_of(v));
            } else if ((v = obj_get(s, "wait_ms")) >= 0) {
                st->kind = STEP_WAIT;
                get_u32(i, n, v, "wait_ms", DEMO_MAX_MS, &st->value);
            } else {
                st->kind = STEP_GAMEPAD;
                st->value = gamepad_word(obj_get(s, "gamepad"));
            }
        }
    }
}

int demo_parse(const char *text, size_t len, const char *shuttle, struct demo *out,
               struct errors *errs) {
    E = errs;
    errors_reset(errs);
    js = text;
    js_len = len;
    if (!utf8_check((const unsigned char *)text, len)) return (int)errs->count;
    if (!json_ok()) return (int)errs->count;
    validate(shuttle);
    if (errs->count) return (int)errs->count;
    extract(shuttle, out);
    return (int)errs->count;
}
