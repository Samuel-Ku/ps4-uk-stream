// Regression test for issue #15: Cyrillic append must decode the full
// UTF-8 codepoint from labelAt(), not just take label[0].
#include <cstdio>
#include <string>
#include "OnscreenKeyboard.h"

int main() {
    cs::OnscreenKeyboard kb;

    // "ДЮНС" — four letters from the Cyrillic half of the on-screen
    // grid. Their labels are multi-byte UTF-8 std::strings; the bug
    // was that ScreenSearch would take label[0] (the first byte, e.g.
    // 0xD0) and append that as if it were a single char, producing
    // invalid UTF-8 and breaking the search input.
    const auto &d  = kb.labelAt(0, 5);  // Д
    const auto &yu = kb.labelAt(3, 1);  // Ю
    const auto &n  = kb.labelAt(1, 7);  // Н
    const auto &s  = kb.labelAt(2, 1);  // С

    // Round-trip "Д" through appendUtf8 vs append(char32_t). They
    // must produce the same buffer.
    cs::OnscreenKeyboard a1;
    a1.append(U'Д');
    cs::OnscreenKeyboard a2;
    a2.appendUtf8(d);
    if (a1.text() != a2.text()) {
        std::fprintf(stderr, "Д round-trip failed: a1=%s a2=%s\n",
                     a1.text().c_str(), a2.text().c_str());
        return 1;
    }

    // Spell a word by walking the grid with appendUtf8.
    cs::OnscreenKeyboard kb2;
    kb2.setText("");
    kb2.appendUtf8(d);
    kb2.appendUtf8(yu);
    kb2.appendUtf8(n);
    kb2.appendUtf8(s);
    const std::string want = "ДЮНС";
    if (kb2.text() != want) {
        std::fprintf(stderr, "spelling failed: got=%s want=%s\n",
                     kb2.text().c_str(), want.c_str());
        return 2;
    }

    // Confirm the buffer is still valid UTF-8 — first byte must be
    // 0xD0 (start of a 2-byte Cyrillic sequence).
    if (kb2.text().empty() ||
        static_cast<unsigned char>(kb2.text()[0]) != 0xD0) {
        std::fprintf(stderr, "first byte is not a UTF-8 Cyrillic lead: 0x%02X\n",
                     kb2.text()[0]);
        return 3;
    }

    // Empty string is a no-op, not a crash.
    kb2.appendUtf8("");
    if (kb2.text() != want) {
        std::fprintf(stderr, "empty appendUtf8 changed the buffer\n");
        return 4;
    }

    // Latin 1-byte path still works.
    kb2.appendUtf8("a");
    if (kb2.text().back() != 'a') {
        std::fprintf(stderr, "latin append failed\n");
        return 5;
    }

    // 3-byte UTF-8 (e.g. U+2014 em-dash) round-trips.
    cs::OnscreenKeyboard em;
    em.appendUtf8("\xE2\x80\x94");
    if (em.text().size() != 3) {
        std::fprintf(stderr, "em-dash size wrong: %zu\n", em.text().size());
        return 6;
    }
    return 0;
}