#  CASA Next Generation Infrastructure
#  Copyright (C) 2021, 2023 AUI, Inc. Washington DC, USA
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.

import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple, Union

import numpy as np
import xarray as xr


def read_part_keys(inpath: str) -> List[Tuple]:
    """
    Reads the partition keys from a Zarr-stored cds.

    :param inpath: path to read from

    :return: partition keys from a cds
    """

    xds_keys = xr.open_zarr(
        os.path.join(inpath, "partition_keys"),
    )

    spw_ids = xds_keys.coords["spw_ids"]
    pol_setup_ids = xds_keys.coords["pol_setup_ids"]
    intents = xds_keys.coords["intents"]

    return list(zip(spw_ids.values, pol_setup_ids.values, intents.values))


def read_subtables(inpath: str, asdm_subtables: bool) -> Dict[str, xr.Dataset]:
    """
    Reads the metainfo subtables from a Zarr-stored cds.

    :param inpath: path to read from

    :return: metainfo subtables from a cds
    """

    metainfo = {}
    metadir = Path(inpath, "metainfo")
    for subt in sorted(metadir.iterdir()):
        if subt.is_dir():
            if not asdm_subtables and subt.name.startswith("ASDM_"):
                logging.debug(f"Not loading ASDM_ subtable {subt.name}...")
                continue

            metainfo[subt.name] = read_xds(subt, consolidated=True)

    return metainfo


def read_partitions(inpath: str, part_keys: List[Tuple]) -> Dict[str, xr.Dataset]:
    """
    Reads all the data partitions a Zarr-stored cds.

    :param inpath: path to read from

    :return: partitions from a cds
    """

    partitions = {}
    partdir = Path(inpath, "partitions")
    xds_cnt = 0
    for part in sorted(partdir.iterdir()):
        if part.is_dir() and part.name.startswith("xds_"):
            xds = read_xds(part, consolidated=True)
            partitions[part_keys[xds_cnt]] = xds
            xds_cnt += 1

    return partitions


def read_xds(
    inpath: str,
    chunks: Union[Dict, None] = None,
    consolidated: bool = True,
    overwrite_encoded_chunks: bool = True,
) -> xr.Dataset:
    """
    Read single xds from zarr storage.

    :param inpath: path to read from
    :param chunks: set chunk size per dimension. Dict is in the form of
    'dim':chunk_size, for example {'time':100, 'baseline':400, 'chan':32, 'pol':1}.
    Default None uses the original chunking in the zarr input.
    :param consolidated: use zarr consolidated metadata.
    :param overwrite_encoded_chunks: drop the zarr chunks encoded for each variable
    when a dataset is loaded with specified chunk sizes.
    """

    xds = xr.open_zarr(
        os.path.join(inpath),
        chunks=chunks,
        consolidated=consolidated,
        overwrite_encoded_chunks=overwrite_encoded_chunks,
    )

    return xds


def read_zarr(
    infile,
    sel_xds=None,
    chunks=None,
    consolidated=True,
    overwrite_encoded_chunks=True,
    **kwargs,
):
    """
    Note: old, initial cngi-io format. To be removed, most likely.
    Read zarr format Visibility data from disk to an ngCASA visibilities dataset
    object consisting of dictionaries of xarray Datasets.

    Parameters
    ----------
    infile : str
        input Visibility filename
    sel_xds : string or list
        Select the ddi to open, for example ['xds0','xds1'] will open the first two ddi. Default None returns everything
    chunks : dict
        sets specified chunk size per dimension. Dict is in the form of
        'dim':chunk_size, for example {'time':100, 'baseline':400, 'chan':32, 'pol':1}.
        Default None uses the original zarr chunking.
    consolidated : bool
        use zarr consolidated metadata capability. Only works for stores that have
        already been consolidated. Default True works with datasets produced by
        convert_ms which automatically consolidates metadata.
    overwrite_encoded_chunks : bool
        drop the zarr chunks encoded for each variable when a dataset is loaded with
        specified chunk sizes.  Default True, only applies when chunks is not None.

    Returns
    -------
    xarray.core.dataset.Dataset
        New xarray Dataset of Visibility data contents
    """

    if chunks is None:
        chunks = "auto"
        # overwrite_encoded_chunks = False
    # print('overwrite_encoded_chunks',overwrite_encoded_chunks)

    infile = os.path.expanduser(infile)
    if sel_xds is None:
        sel_xds = os.listdir(infile)
    sel_xds = list(np.atleast_1d(sel_xds))

    # print(os.path.join(infile, 'DDI_INDEX'))
    mxds = xr.open_zarr(
        os.path.join(infile, "DDI_INDEX"),
        chunks=chunks,
        consolidated=consolidated,
        overwrite_encoded_chunks=overwrite_encoded_chunks,
    )

    for part in os.listdir(os.path.join(infile, "global")):
        xds_temp = xr.open_zarr(
            os.path.join(infile, "global/" + part),
            chunks=chunks,
            consolidated=consolidated,
            overwrite_encoded_chunks=overwrite_encoded_chunks,
        )
        xds_temp = _fix_dict_for_ms(part, xds_temp)
        mxds.attrs[part] = xds_temp.compute()

    for part in os.listdir(infile):
        if ("xds" in part) and (part in sel_xds):
            xds_temp = xr.open_zarr(
                os.path.join(infile, part),
                chunks=chunks,
                consolidated=consolidated,
                overwrite_encoded_chunks=overwrite_encoded_chunks,
            )
            xds_temp = _fix_dict_for_ms(part, xds_temp)
            mxds.attrs[part] = xds_temp

    return mxds


def _fix_dict_for_ms(name, xds):
    xds.attrs["column_descriptions"] = xds.attrs["column_descriptions"][0]
    xds.attrs["info"] = xds.attrs["info"][0]

    if "xds" in name:
        xds.column_descriptions["UVW"]["shape"] = np.array(
            xds.column_descriptions["UVW"]["shape"].split(",")
        ).astype(int)

    if "spectral_window" == name:
        xds.column_descriptions["CHAN_FREQ"]["keywords"]["MEASINFO"][
            "TabRefCodes"
        ] = np.array(
            xds.column_descriptions["CHAN_FREQ"]["keywords"]["MEASINFO"][
                "TabRefCodes"
            ].split(",")
        ).astype(
            int
        )
        xds.column_descriptions["REF_FREQUENCY"]["keywords"]["MEASINFO"][
            "TabRefCodes"
        ] = np.array(
            xds.column_descriptions["REF_FREQUENCY"]["keywords"]["MEASINFO"][
                "TabRefCodes"
            ].split(",")
        ).astype(
            int
        )

    if "antenna" == name:
        xds.column_descriptions["OFFSET"]["shape"] = np.array(
            xds.column_descriptions["OFFSET"]["shape"].split(",")
        ).astype(int)
        xds.column_descriptions["POSITION"]["shape"] = np.array(
            xds.column_descriptions["POSITION"]["shape"].split(",")
        ).astype(int)

    if "feed" == name:
        xds.column_descriptions["POSITION"]["shape"] = np.array(
            xds.column_descriptions["POSITION"]["shape"].split(",")
        ).astype(int)

    if "observation" == name:
        xds.column_descriptions["TIME_RANGE"]["shape"] = np.array(
            xds.column_descriptions["TIME_RANGE"]["shape"].split(",")
        ).astype(int)

    return xds
