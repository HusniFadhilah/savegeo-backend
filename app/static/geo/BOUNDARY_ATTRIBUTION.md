# Province boundary data

`indonesia_provinces_38.geojson` is assembled from the province files in
[Indonesia-Geojson-2026](https://github.com/mashadinoor/Indonesia-Geojson-2026),
which credits the Badan Informasi Geospasial (BIG) as its geometry source. Province
codes follow Kepmendagri No. 100.1.1-6117 Tahun 2022. The source repository
provides its files under the MIT license; its copyright and license are retained
at the source link above.

Copyright (c) 2024 Ardian Saputra Hasibuan

Permission is hereby granted, free of charge, to any person obtaining a copy of
this data and associated documentation files (the "Data"), to deal in the Data
without restriction, including without limitation the rights to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies of the Data,
and to permit persons to whom the Data is furnished to do so, subject to the
following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Data.

THE DATA IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT.

Invalid polygon rings were repaired with Shapely `make_valid` before packaging.
The countrywide FIRMS mask uses the separate checked-in `indonesia_whole.geojson`
outline. These display and filtering boundaries do not replace legally certified
administrative boundary records.
