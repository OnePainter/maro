import gzip
import os
import shutil
import time
from typing import List

import aria2p
import pandas as pd
from yaml import safe_load

from maro.cli.data_pipeline.base import DataPipeline, DataTopology
from maro.cli.data_pipeline.utils import StaticParameter
from maro.utils.logger import CliLogger

logger = CliLogger(name=__name__)


class DataCenterPipeline(DataPipeline):
    """Generate data_center data and other necessary files for the specified topology.

    The files will be generated in ~/.maro/data/data_center.
    """

    _download_file_name = "AzurePublicDatasetLinksV2.txt"

    _vm_table_file_name = "vmtable.csv.gz"
    _clean_file_name = "vmtable.csv"

    def __init__(self, topology: str, source: str, is_temp: bool = False):
        super().__init__(scenario="data_center", topology=topology, source=source, is_temp=is_temp)

        self._vm_table_file = os.path.join(self._download_folder, self._vm_table_file_name)

        self.aria2 = aria2p.API(
            aria2p.Client(
                host="http://localhost",
                port=6800,
                secret=""
            )
        )
        self._download_file_list = []

    def download(self, is_force: bool = False):
        # Download text with all urls.
        super().download(is_force=is_force)
        if os.path.exists(self._download_file):
            # Download vm_table and cpu_reading
            logger.info_green("Downloading vmtable and cpu readings.")
            self._aria2p_download(is_force=is_force)
        else:
            logger.warning(f"Not found downloaded source file: {self._download_file}.")

    def _aria2p_download(self, is_force: bool = False) -> List[list]:
        """Read from the text file which contains urls and use aria2p to download.

        Args:
            is_force (bool): Is force or not.
        """

        # Download parts of cpu reading files.
        num_files = 4
        # Open the txt file which contains all the required urls.
        with open(self._download_file, mode="r", encoding="utf-8") as urls:
            for remote_url in urls.read().splitlines():
                # Get the file name.
                file_name = remote_url.split('/')[-1]
                # Two kinds of required files "vmtable" and "vm_cpu_readings-" start with vm.
                if file_name.startswith("vm"):
                    if file_name.startswith("vm_cpu_readings"):
                        num_files -= 1
                    if (not is_force) and os.path.exists(self._vm_table_file):
                        logger.info_green("File already exists, skipping download.")
                        self.aria2.add_uris(uris=[remote_url], options={'dir': f"{self._download_folder}"})
                    else:
                        logger.info_green(f"Downloading vmtable from {remote_url} to {self._vm_table_file}.")
        self._check_all_download_completed()

    def _check_all_download_completed(self):
        """Check all download tasks are completed and remove the ".aria2" files."""

        while 1:
            downloads = self.aria2.get_downloads()
            if len(downloads) == 0:
                logger.info_green("Doesn't exist any pending file.")
                break

            if all([download.is_complete for download in downloads]):
                # Remove temp .aria2 files.
                self.aria2.remove(downloads)
                logger.info_green("Download finished.")
                break

            for download in downloads:
                logger.info_green(f"{download.name}, {download.status}, {download.progress:.2f}%")
            logger.info_green("-" * 60)
            time.sleep(5)

    def clean(self):
        """Unzip the csv file and process it for building binary file."""
        super().clean()
        logger.info_green("Cleaning VM data.")
        if os.path.exists(self._vm_table_file):
            # Unzip gz file.
            unzip_vm_table_file = os.path.join(self._clean_folder, self._clean_file_name)
            logger.info_green("Unzip start.")
            with gzip.open(self._vm_table_file, mode="r") as f_in:
                logger.info_green(
                    f"Unzip {self._clean_file_name} from {self._vm_table_file} to {unzip_vm_table_file}."
                )
                with open(unzip_vm_table_file, "w") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            logger.info_green("Unzip finished.")
            # Preprocess vmtable.
        else:
            logger.warning(f"Not found downloaded source file: {self._vm_table_file}.")

    def _process_vm_table(self, unzip_vm_table_file: str) -> pd.DataFrame:
        """Process vmtable file."""

        headers = [
            'vmid', 'subscriptionid', 'deploymentid', 'vmcreated', 'vmdeleted', 'maxcpu', 'avgcpu', 'p95maxcpu',
            'vmcategory', 'vmcorecountbucket', 'vmmemorybucket'
        ]
        required_headers = ['vmid', 'vmcreated', 'vmdeleted', 'vmcorecountbucket', 'vmmemorybucket']

        vm_table = pd.read_csv(unzip_vm_table_file, header=None, index_col=False, names=headers)
        vm_table = vm_table.loc[:, required_headers]

        vm_table['vmcreated'] = pd.to_numeric(vm_table['vmcreated'], errors="coerce", downcast="integer") // 300
        vm_table['vmdeleted'] = pd.to_numeric(vm_table['vmdeleted'], errors="coerce", downcast="integer") // 300

        vm_table['vmcorecountbucket'] = pd.to_numeric(
            vm_table['vmcorecountbucket'], errors="coerce", downcast="integer"
        )
        vm_table['vmmemorybucket'] = pd.to_numeric(vm_table['vmmemorybucket'], errors="coerce", downcast="integer")
        vm_table.dropna(inplace=True)

        vm_table = vm_table[vm_table['vmdeleted'] <= 43]
        vm_table['lifetime'] = vm_table['vmdeleted'] - vm_table['vmcreated']
        vm_table = vm_table.sort_values(by='vmcreated', ascending=True)
        return vm_table

    def _preprocess(self, unzip_vm_table_file: str):
        vm_table = self._process_vm_table(unzip_vm_table_file=unzip_vm_table_file)
        with open(self._clean_file, mode="w", encoding="utf-8", newline="") as f:
            vm_table.to_csv(f, index=False, header=True)


class DataCenterTopology(DataTopology):
    def __init__(self, topology: str, source: str, is_temp=False):
        super().__init__()
        self._data_pipeline["vm_data"] = DataCenterPipeline(topology=topology, source=source, is_temp=is_temp)


class DataCenterProcess:
    """Contains all predefined data topologies of data_center scenario."""

    meta_file_name = "source_urls.yml"
    meta_root = os.path.join(StaticParameter.data_root, "data_center/meta")

    def __init__(self, is_temp: bool = False):
        self.topologies = {}
        self.meta_root = os.path.expanduser(self.meta_root)
        self._meta_path = os.path.join(self.meta_root, self.meta_file_name)

        with open(self._meta_path) as fp:
            self._conf = safe_load(fp)
            for topology in self._conf["vm_data"].keys():
                self.topologies[topology] = DataCenterTopology(
                    topology=topology,
                    source=self._conf["vm_data"][topology]["remote_url"],
                    is_temp=is_temp
                )
