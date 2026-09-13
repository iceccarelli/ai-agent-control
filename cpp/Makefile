# carrycore — one translation unit, no dependencies, C ABI.
#
# Built on demand by bot/tools/carry_core.py and by the test suite. There is no
# package to install and nothing to vendor: g++ and this file.
CXX      ?= g++
CXXFLAGS ?= -O2 -std=c++17 -fPIC -Wall -Wextra -pthread
OUT      ?= ../bot/lib/libcarrycore.so

all: $(OUT)

$(OUT): carrycore.cpp carrycore.h
	@mkdir -p $(dir $(OUT))
	$(CXX) $(CXXFLAGS) -shared -o $(OUT) carrycore.cpp

clean:
	rm -f $(OUT)

.PHONY: all clean
