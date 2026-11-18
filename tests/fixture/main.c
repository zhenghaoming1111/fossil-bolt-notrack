#include <limits.h>
#include <stdio.h>

int dispatch_plain(unsigned index);
int dispatch_notrack(unsigned index);

int main(void) {
  const unsigned inputs[] = {0, 1, 2, 3, UINT_MAX};
  const int expected[] = {11, 22, 33, -1, -1};
  for (unsigned i = 0; i < sizeof(inputs) / sizeof(inputs[0]); ++i) {
    const int plain = dispatch_plain(inputs[i]);
    const int notrack = dispatch_notrack(inputs[i]);
    if (plain != expected[i] || notrack != expected[i]) {
      fprintf(stderr, "index=%u expected=%d plain=%d notrack=%d\n",
              inputs[i], expected[i], plain, notrack);
      return 1;
    }
  }
  puts("PASS: plain and notrack dispatch; all table entries and defaults.");
  return 0;
}
