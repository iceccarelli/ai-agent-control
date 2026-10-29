# carrycore — one translation unit, no dependencies, C ABI.
#
# Built on demand by bot/tools/carry_core.py and by the test suite. There is no
# package to install and nothing to vendor: g++ and this file.
CXX      ?= g++
CXXFLAGS ?= -O2 -std=c++17 -fPIC -Wall -Wextra -pthread
OUT      ?= ../bot/lib/libcarrycore.so

# The core promises the same arithmetic as the Python reference, to the cent.
# A fused multiply-add rounds once and cannot overflow in the middle, so on a
# target with FMA `BIG * 3 - inf` is -inf where Python's `inf - inf` is NaN and
# the gate answered the wrong reason. Appended AFTER $(CXXFLAGS) so a flag
# inherited from the environment (-march=native, -O3, ...) cannot undo it.
FPFLAGS  := -ffp-contract=off

all: $(OUT)

$(OUT): carrycore.cpp carrycore.h
	@mkdir -p $(dir $(OUT))
	$(CXX) $(CXXFLAGS) $(FPFLAGS) -shared -o $(OUT) carrycore.cpp

clean:
	rm -f $(OUT)

.PHONY: all clean
