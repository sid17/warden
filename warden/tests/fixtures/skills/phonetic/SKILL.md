---
name: phonetic
description: "Spell words in the NATO phonetic alphabet."
---

# Phonetic

Given a word, spell it out in the NATO phonetic alphabet (alpha, bravo,
charlie, ...).

## Alphabet

| Letter | Word    | Letter | Word     | Letter | Word     |
|--------|---------|--------|----------|--------|----------|
| A      | alpha   | J      | juliett  | S      | sierra   |
| B      | bravo   | K      | kilo     | T      | tango    |
| C      | charlie | L      | lima     | U      | uniform  |
| D      | delta   | M      | mike     | V      | victor   |
| E      | echo    | N      | november | W      | whiskey  |
| F      | foxtrot | O      | oscar    | X      | x-ray    |
| G      | golf    | P      | papa     | Y      | yankee   |
| H      | hotel   | Q      | quebec   | Z      | zulu     |
| I      | india   | R      | romeo    |        |          |

## Behavior

For each letter of the input word, output its phonetic word. Example: `TC` →
`tango charlie`. Lowercase the output words; preserve the input order.
