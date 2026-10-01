# Instrument format signatures

These tiny files are safe reference examples used to test instrument
recognition. They are **not** production inputs and the uploader does not read
this folder. See `manifest.json` for the source and acquisition method of every
instrument.

All five configured instruments are represented. The three COM-port instruments
are:

- `NO2-CAPS` (`no2-caps.txt`)
- `NEPH-PM25` (`neph-pm25.txt`)
- `CO2-LICOR` (`co2-licor.xml`)

`SMPS` and `BC-MA200` are file-based on the field laptop, so they do not get a
COM-port assignment. Their examples are included because the same pre-Bronze
validator protects all five instrument prefixes.

The NEPH, NO2, LI-COR and SMPS references are short extracts from the supplied
field-data archive. That archive's black-carbon directory was empty, and the BC
AWS prefix currently contains no Bronze data. Therefore `black-carbon-ma200.csv`
is explicitly marked as a format-only fixture—not an actual measurement. Replace
it with a few unmodified header/data rows when a real MA200 capture is available.

From the repository directory in VS Code, check these examples with:

```powershell
py -3 scripts\field\identify_instrument_data.py sample-data
```

To predict the currently attached COM ports, stop the scheduled serial logger
first (only one process can open a COM port), then run:

```powershell
py -3 scripts\field\acquire_serial.py --detect-ports --probe-seconds 20
```

Detection is report-only. Review the predicted instrument names before saving:

```powershell
py -3 scripts\field\acquire_serial.py --apply-detected-ports --probe-seconds 20
```
