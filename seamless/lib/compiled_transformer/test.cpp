#include <cmath>

extern "C" double transform(int a, int b) {
  float x = 0;
  for (int n = 0; n < 1000; n++) {
    for (int m = 0; m < 1000; m++) {
      x += (a + m) * (b + n) / 10e6;
    }
  }
  int xx = x;
  return xx % 1000 + M_PI;
}
