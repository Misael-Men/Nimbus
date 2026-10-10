#!/bin/bash
# Compiles against the minimal API stubs in stubs/ (only the jar's own classes are packaged).
set -e
cd "$(dirname "$0")"
rm -rf build && mkdir -p build/stubs build/classes dist
javac --release 21 -d build/stubs $(find stubs -name '*.java')
javac --release 21 -cp build/stubs -d build/classes $(find src/main/java -name '*.java')
cp src/main/resources/* build/classes/
jar --create --file dist/AdaptiveDistance.jar -C build/classes .
rm -rf build
echo "Built dist/AdaptiveDistance.jar"
